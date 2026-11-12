"""Exact archived prices, paired non-overlapping scores, and fixed-notional paper PnL."""
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from candle_store import SOURCE, validate
from . import store
from .core import ENGINES, canonical


def prices(path, cutoff, horizon, now, symbol='BTC-USD'):
    if symbol != 'BTC-USD':
        from .market import prices as asset_prices
        return asset_prices(path, symbol, cutoff, horizon, now)
    if now < cutoff+horizon+60:
        return None
    starts = (cutoff-300, cutoff+300, cutoff+horizon-300)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        rows = [c.execute('SELECT * FROM candles_5m WHERE start_ts=?', (t,)).fetchone() for t in starts]
    if any(r is None for r in rows):
        return None
    for r in rows:
        if r['source'] != SOURCE or r['fetched_at'] > now:
            return None
        validate([r['start_ts'], r['low'], r['high'], r['open'], r['close'], r['volume']],
                 r['start_ts'], r['start_ts']+300, r['fetched_at'])
    ref, entry, end = rows
    return {'reference_price': ref['close'], 'end_price': end['close'],
            'price_up': int(end['close'] > ref['close']), 'flat': end['close'] == ref['close'],
            'forecast_return': end['close']/ref['close']-1,
            'paper_entry_at': cutoff+300, 'paper_exit_at': cutoff+horizon,
            'paper_entry_price': entry['open'], 'paper_return': end['close']/entry['open']-1,
            'price_source': SOURCE, 'candles': [dict(r) for r in rows]}


def evaluate(db, candle_db, clock=time.time):
    now = clock()
    with store.connect(db, True) as c:
        config = json.loads(c.execute('SELECT config_json FROM experiment').fetchone()[0])
        pending = c.execute('SELECT cutoff FROM cohorts WHERE cutoff NOT IN (SELECT cutoff FROM outcomes)').fetchall()
    saved = unavailable = 0
    for row in pending:
        data = prices(candle_db, row[0], config['horizon_seconds'], now, config['symbol'])
        if data is None:
            unavailable += 1
            continue
        with store.connect(db) as c, c:
            saved += c.execute('INSERT OR IGNORE INTO outcomes VALUES(?,?,?)',
                               (row[0], now, canonical(data))).rowcount
    return {'evaluated': saved, 'pending_or_price_missing': unavailable}


def scores(pairs):
    n = len(pairs)
    if not n:
        return {'n': 0, 'accuracy': None, 'brier': None, 'logloss': None}
    eps = 1e-15
    return {'n': n, 'accuracy': sum((p >= .5) == bool(y) for p, y in pairs)/n,
            'brier': sum((p-y)**2 for p, y in pairs)/n,
            'logloss': -sum(y*math.log(max(eps, min(1-eps, p))) +
                           (1-y)*math.log(max(eps, min(1-eps, 1-p))) for p, y in pairs)/n}


def report(db):
    with store.connect(db, True) as c:
        c.execute('BEGIN')
        config = json.loads(c.execute('SELECT config_json FROM experiment').fetchone()[0])
        cohorts = [dict(r) for r in c.execute('SELECT cutoff,mode,status FROM cohorts ORDER BY cutoff')]
        calls = [dict(r) for r in c.execute('SELECT * FROM calls ORDER BY cutoff,engine')]
        outcomes = {r['cutoff']: json.loads(r['outcome_json']) for r in c.execute('SELECT * FROM outcomes')}
    results = {(r['cutoff'], r['engine']): json.loads(r['result_json']) if r['result_json'] else
               {'status': r['status'], 'probability_up': None} for r in calls}
    targets = [r['cutoff'] for r in cohorts]
    common = [t for t in targets if t in outcomes and all(results[t, e]['status'] == 'ok' for e in ENGINES)]
    # Fixed epoch anchor, independent of predictions, outcomes, missing calls or first sample.
    primary = [t for t in common if t % config['horizon_seconds'] == 0]
    paper_targets = [t for t in targets if t in outcomes and t % config['horizon_seconds'] == 0]
    output = {'schema': 'phase8c.report.v1', 'mode': cohorts[0]['mode'] if cohorts else None,
              'warning': 'OFFLINE FIXTURES: NOT MODEL PERFORMANCE' if cohorts and cohorts[0]['mode'] == 'offline' else None,
              'config': config, 'recorded_cutoffs': len(targets), 'evaluated_cutoffs': len(outcomes),
              'missing_scheduled_cutoffs_between_first_last': ((max(targets)-min(targets))//config['interval_seconds']+1-len(targets)) if targets else 0,
              'paired_hourly_n': len(common), 'paired_nonoverlap_n': len(primary),
              'paired_nonoverlap_cutoffs': primary, 'flat_outcomes': sum(o['flat'] for o in outcomes.values()),
              'baseline_p_half_nonoverlap': scores([(.5, outcomes[t]['price_up']) for t in primary]), 'engines': {}}
    for engine in ENGINES:
        engine_calls = [r for r in calls if r['engine'] == engine]
        all_valid = [t for t in targets if t in outcomes and results[t, engine]['status'] == 'ok']
        pnl, trades, opportunities = 0., 0, len(paper_targets)
        for t in paper_targets:
            r = results[t, engine]
            if r['status'] != 'ok':
                continue
            p = r['probability_up']
            sign = 1 if p >= config['trade_threshold'] else (-1 if p <= 1-config['trade_threshold'] else 0)
            if sign:
                trades += 1
                pnl += config['paper_notional_usd']*(sign*outcomes[t]['paper_return']-config['round_trip_cost_bps']/10000)
        # All paid calls count, including overlapping calls, failures and pending outcomes.
        conservative_cost = sum(r['reserved_usd'] for r in engine_calls)
        valid = sum(results[t, engine]['status'] == 'ok' for t in targets)
        output['engines'][engine] = {
            'all_available_unpaired': scores([(results[t, engine]['probability_up'], outcomes[t]['price_up']) for t in all_valid]),
            'paired_hourly_descriptive': scores([(results[t, engine]['probability_up'], outcomes[t]['price_up']) for t in common]),
            'paired_nonoverlap_primary': scores([(results[t, engine]['probability_up'], outcomes[t]['price_up']) for t in primary]),
            'valid_calls': valid, 'unavailable_rate': 1-valid/len(targets) if targets else None,
            'error_or_late_count': sum(results[t, engine]['status'] in ('error', 'late', 'reserved') for t in targets),
            'statuses': {s: sum(results[t, engine]['status'] == s for t in targets)
                         for s in sorted(set(results[t, engine]['status'] for t in targets))},
            'paper': {'opportunities': opportunities, 'trades': trades,
                      'skip_rate': 1-trades/opportunities if opportunities else None,
                      'pnl_after_trading_cost_usd': pnl, 'api_reserved_upper_estimate_usd': conservative_cost,
                      'pnl_after_trading_and_all_reserved_api_cost_usd': pnl-conservative_cost,
                      'api_cost_basis': 'accounting reservation; NOT a price cap for unpriced calls',
                      'unknown_cost_calls': sum(results[t, engine].get('cost_kind') == 'unknown' for t in targets),
                      'pending_outcomes': len(targets)-len(outcomes)}}
    return output
