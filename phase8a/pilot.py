"""Manual five-asset pilot: one frozen cutoff, <=20 calls, durable no-replay gate.

This command never installs schedules or changes the production live switch.
The explicit Kairos exception is confined to this manual batch, not tick/predict.
"""
import argparse
import copy
import fcntl
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from news.store import snapshot
from . import adapters, runtime, store, market, evaluation
from .core import SYMBOLS, ENGINES, canonical, digest, freeze, validate_config


def prepare(config, news_db, cutoff, allow_unpriced_kairos=False):
    snap = snapshot(news_db, cutoff, hours=config['lookback_hours'], deduplicate=False)
    plans, ceiling = {}, 0.0
    for symbol in SYMBOLS:
        c = copy.deepcopy(config)
        c.update(symbol=symbol, interval_seconds=300, live_enabled=True, max_calls=4)
        validate_config(c)
        frozen = freeze(snap, c, cutoff)
        if not frozen['state']['news']:
            raise ValueError('no_eligible_news_' + symbol)
        if any(r['enabled'] and r['stale'] and r['feed_id'] in c['allowed_sources'] for r in snap['coverage']):
            raise ValueError('source_stale')
        adapters.preflight(c, frozen['state'], allow_unpriced_kairos=allow_unpriced_kairos)
        bounds = {}
        for engine in ENGINES:
            settings = c['engines'][engine]
            if engine == 'kairos' and allow_unpriced_kairos:
                bounds[engine] = None
                continue
            n = len(canonical(adapters.request(engine, frozen['state'], c)).encode()) + 512
            bounds[engine] = (n*settings['input_per_million_usd'] + c['max_output_tokens']*settings['output_per_million_usd'])/1e6 + settings['per_call_usd']
            ceiling += bounds[engine]
        plans[symbol] = {'config': c, 'frozen': frozen, 'bounds_usd': bounds}
    return plans, ceiling


def summarize(directory):
    directory = Path(directory)
    manifest = json.loads((directory/'manifest.json').read_text())
    output = {'cutoff': manifest['cutoff'], 'target_end': manifest['cutoff']+manifest['horizon_seconds'],
              'purpose': 'prospective_real_news_pilot_not_performance_evidence',
              'known_cost_ceiling_usd': manifest['known_cost_ceiling_usd'],
              'kairos_cost_unknown_authorized': manifest['allow_unpriced_kairos'], 'assets': {}}
    known_cost, unknown = 0., 0
    for symbol in SYMBOLS:
        db = directory/(symbol+'.db')
        if not db.exists():
            output['assets'][symbol] = {'status': 'not_dispatched'}
            continue
        with store.connect(db, True) as c:
            row = c.execute('SELECT input_json FROM cohorts').fetchone()
            if row is None:
                output['assets'][symbol] = {'status': 'not_dispatched'}
                continue
            frozen = json.loads(row[0])
            results = [json.loads(r[0]) if r[0] else {'status': 'reserved_or_pending'}
                       for r in c.execute('SELECT result_json FROM calls ORDER BY engine')]
        news = frozen['state']['news']
        compact = []
        for r in results:
            compact.append({k: r.get(k) for k in ('engine', 'status', 'direction', 'probability_up', 'model_version',
                          'latency_ms', 'cost_usd', 'cost_kind', 'error', 'news_set_id', 'input_id')})
            if r.get('cost_usd') is None:
                unknown += 1
            else:
                known_cost += r['cost_usd']
        output['assets'][symbol] = {'news_count': len(news), 'direct_news_count': sum(n['relevance']=='direct' for n in news),
                                   'news_set_id': frozen['state']['news_set_id'], 'news': news, 'results': compact,
                                   'evaluation': evaluation.report(db)}
    output.update(known_cost_estimate_usd=known_cost, unknown_cost_calls=unknown,
                  total_cost_usd=None if unknown else known_cost)
    (directory/'summary.json').write_text(canonical(output)+'\n')
    return output


def run_batch(config, news_db, directory, cutoff, *, known_budget_usd=.99, allow_unpriced_kairos=False,
              clock=time.time, call=None, prepared=None):
    if not 0 < known_budget_usd <= .99:
        raise ValueError('pilot_budget_limit')
    if cutoff % 300 or not cutoff <= clock() <= cutoff+config['max_start_delay_seconds']:
        raise ValueError('pilot_requires_current_aligned_cutoff')
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(directory/'run.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A started batch is NEVER resumed, even after a crash before the first HTTP call.
        if (directory/'manifest.json').exists():
            raise ValueError('pilot_already_started_no_retry')
        plans, ceiling = prepared if prepared is not None else prepare(config, news_db, cutoff, allow_unpriced_kairos)
        if ceiling > known_budget_usd:
            raise ValueError('aggregate_known_price_ceiling_exceeded')
        manifest = dict(cutoff=cutoff, horizon_seconds=config['horizon_seconds'], symbols=SYMBOLS,
                        known_cost_ceiling_usd=ceiling, known_budget_usd=known_budget_usd,
                        allow_unpriced_kairos=allow_unpriced_kairos, max_calls=20,
                        production_live_enabled=config['live_enabled'],
                        plans=plans, plan_hash=digest(plans))
        with open(directory/'manifest.json', 'x') as f:
            f.write(canonical(manifest)+'\n'); f.flush(); os.fsync(f.fileno())
        def worker(symbol):
            plan = plans[symbol]
            return runtime.run(directory/(symbol+'.db'), news_db, plan['config'], cutoff, 'live', clock, call,
                               allow_unpriced_kairos=allow_unpriced_kairos, frozen_input=plan['frozen'])
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(worker, SYMBOLS))
    return summarize(directory)


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--config', required=True)
    run.add_argument('--news-db', required=True)
    run.add_argument('--directory', required=True)
    run.add_argument('--cutoff', type=int, required=True)
    run.add_argument('--allow-unpriced-kairos', action='store_true')
    run.add_argument('--known-budget-usd', type=float, default=.99)
    report = sub.add_parser('report'); report.add_argument('--directory', required=True)
    score = sub.add_parser('evaluate')
    score.add_argument('--directory', required=True)
    score.add_argument('--btc-candles-db', required=True)
    score.add_argument('--fetch-prices', action='store_true')
    args = p.parse_args()
    try:
        if args.command == 'run':
            c = validate_config(json.loads(Path(args.config).read_text()))
            out = run_batch(c, args.news_db, args.directory, args.cutoff,
                            allow_unpriced_kairos=args.allow_unpriced_kairos, known_budget_usd=args.known_budget_usd)
        elif args.command == 'report':
            out = summarize(args.directory)
        else:
            directory = Path(args.directory)
            manifest = json.loads((directory/'manifest.json').read_text())
            for symbol in SYMBOLS:
                db = directory/(symbol+'.db')
                prices_db = directory/'asset-prices.sqlite3'
                if symbol != 'BTC-USD' and args.fetch_prices:
                    market.fetch(prices_db, symbol, manifest['cutoff'], manifest['horizon_seconds'])
                evaluation.evaluate(db, args.btc_candles_db if symbol == 'BTC-USD' else prices_db)
            out = summarize(directory)
        print(canonical(out))
    except Exception:
        print(canonical({'status': 'error', 'error': 'pilot_failed_no_automatic_retry'}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
