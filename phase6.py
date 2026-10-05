"""Prospective Phase 6 storage and runner; no writes to Phase 4/4R/5 tables.

Runtime entry is called only under next_stages' shared flock. Interrupted slots
are never sent again; snapshot reservation makes the entire hour idempotent.
"""
import fcntl
import hashlib
import json
import math
import re
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from phase4 import DB_PATH, ROOT, config_hash, now
from volatility import CLASSES, probabilities, winner
from phase6_methods import BASE_METHODS, METHODS, VERSION, extract, rule, output, ensembles, logistic


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@contextmanager
def database():
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.row_factory = sqlite3.Row
        yield c


def migrate():
    with database() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS phase6_config (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), config_json TEXT NOT NULL,
          config_hash TEXT NOT NULL, started_at TEXT NOT NULL, start_target INTEGER NOT NULL,
          code_sha TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS phase6_events (
          target INTEGER PRIMARY KEY, started_at TEXT NOT NULL, context TEXT,
          snapshot_hash TEXT, features_json TEXT, reference_close REAL,
          gate TEXT NOT NULL, reason TEXT, single_class TEXT, active_votes INTEGER,
          gate_json TEXT, actual_return REAL, actual_direction TEXT, evaluated_at TEXT,
          target_raw_json TEXT);
        CREATE TABLE IF NOT EXISTS phase6_calls (
          target INTEGER NOT NULL, slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 10),
          status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
          class TEXT, probabilities_json TEXT, model_version TEXT, raw_json TEXT,
          error TEXT, cache_ref TEXT, PRIMARY KEY(target,slot));
        CREATE TABLE IF NOT EXISTS phase6_predictions (
          target INTEGER NOT NULL, method TEXT NOT NULL, status TEXT NOT NULL,
          started_at TEXT NOT NULL, completed_at TEXT, direction TEXT, p_up REAL, p_down REAL,
          version TEXT NOT NULL, model_version TEXT, raw_json TEXT, reason TEXT, audit_json TEXT,
          correct INTEGER, brier REAL, signed_pnl REAL, PRIMARY KEY(target,method));
        ''')


def design(volatility_config, model):
    # This source object is frozen before any live call, including all rule choices.
    return dict(version=VERSION, gate={'predictor': 'openai', 'single': 'ACTIVE',
        'repeats': 10, 'modal': 'ACTIVE', 'active_votes_min': 8, 'valid_required': 10,
        'tie_order': list(CLASSES), 'volatility_config': volatility_config, 'model': model},
        methods={m: VERSION for m in METHODS}, target='close(target+1h) / reference completed 1h close - 1',
        target_units='percent', flat='unscored accuracy/Brier; zero PnL',
        snapshot='exact Phase 4/Phase 3 stored text; deterministic rounded fields',
        stop={'accepted_gates': 96, 'optional_stopping': False},
        deadline_seconds=600, provider_timeout_seconds=120, repeat_workers=10, missing='no retry, backfill, imputation or replacement',
        rules={'momentum_1h': 'previous close-to-close 1h sign; zero unavailable',
               'momentum_short': 'sign(mean(5m and 15m returns from 5m frame))',
               'reversal_1h': 'opposite previous 1h return sign', 'always_up': 'UP',
               'breakout': 'last 5m close > prior 11 highs / < prior 11 lows; else unavailable',
               'trend': 'SMA20/SMA50 and normalized MACD histogram sign vote; tie unavailable',
               'order_flow': '60m aggressive buy_ratio minus 0.5 sign',
               'order_book': '60m imb10 minus 0.5 and micro_offset sign vote; tie unavailable'},
        logistic={'training': 'expanding prior evaluated phase2/3 and phase6 snapshots; deduplicate target',
                  'minimum': 100, 'minimum_per_class': 10, 'strict': 'target+3600 < cutoff AND evaluated_at < cutoff',
                  'standardize': 'training only', 'l2': .01, 'steps': 400, 'learning_rate': .1},
        ensemble={'vote_minimum': 3, 'probability_minimum': 2, 'vote_tie': 'unavailable',
                  'probability_tie': 'UP', 'members': list(BASE_METHODS), 'rule_probabilities': 'none'},
        simulation='unit long/short for target 1h; no fees, spread, leverage or sizing',
        paired={'minimum_common_events': 20, 'test': 'two-sided exact McNemar; exploratory, unadjusted'},
        framing='ACTIVE is not assumed easier; alternative signals seek prospective conditional edge')


def implementation_hashes():
    return {name: digest((ROOT/name).read_text()) for name in (
        'phase6.py', 'phase6_methods.py', 'phase4_runner.py', 'volatility.py',
        'next_stages.py', 'predict_gpt.py', 'indicators.py', 'microstructure.py')}


def historical_frequency(c):
    result = dict(manifest_hours=0, valid_ten_shot_hours=0, single_active=0,
                  accepted=0, scheduled_hours=None, gate_rate=None, estimated_days_for_96=None)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'phase4r_sources','phase4r_runs'} <= tables:
        return result
    targets = []
    for saved in c.execute('SELECT source_id,source_json FROM phase4r_sources ORDER BY source_id').fetchall():
        source = json.loads(saved['source_json'])
        if source['predictor'] != 'openai':
            continue
        targets.append(source['target_candle_time'])
        single = winner(probabilities([source['p_quiet'],source['p_normal'],source['p_active']]))
        result['single_active'] += single=='ACTIVE'
        repeated = c.execute("SELECT * FROM phase4r_runs WHERE source_id=? AND status='complete' ORDER BY repeat_no", (saved['source_id'],)).fetchall()
        if len(repeated) != 10:
            continue
        classes = [winner(probabilities([r['p_quiet'],r['p_normal'],r['p_active']])) for r in repeated]
        result['valid_ten_shot_hours'] += 1
        result['accepted'] += gate(single,classes)[0]
    result['manifest_hours'] = len(targets)
    if targets:
        hours = (max(targets)-min(targets))//3600+1
        result['scheduled_hours'] = hours
        result['gate_rate'] = result['accepted']/hours
        if result['accepted']:
            result['estimated_days_for_96'] = 96/result['gate_rate']/24
    result['interpretation'] = 'Historical repeat audit only; missing ensembles excluded; future frequency unknown'
    return result


def start(clock=None):
    """Explicit operator action; enables only future targets, makes no provider calls."""
    from phase4 import load_config
    from predict_gpt import MODEL
    clock = time.time() if clock is None else clock
    migrate()
    with database() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM phase6_config').fetchone():
            return
        stage = c.execute("SELECT 1 FROM experiment_transitions WHERE stage='5'").fetchone()
        if stage is None:
            raise RuntimeError('Start only after Phase 5 is enabled')
        cfg = design(load_config(), MODEL)
        cfg['historical_frequency'] = historical_frequency(c)
        cfg['implementation_hashes'] = implementation_hashes()
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT, text=True)
        if dirty.strip():
            raise RuntimeError('Commit implementation before freezing Phase 6')
        timestamp = datetime.fromtimestamp(clock, timezone.utc).isoformat()
        c.execute('INSERT INTO phase6_config VALUES (1,?,?,?,?,?)',
                  (json.dumps(cfg, sort_keys=True), config_hash(cfg), timestamp, (int(clock)//3600+1)*3600, sha))


def gate(single, repeated):
    if single != 'ACTIVE':
        return False, 'Single-shot is not ACTIVE', 0
    if len(repeated) != 10 or any(x not in CLASSES for x in repeated):
        return False, 'Need ten valid repeated classifications', sum(x=='ACTIVE' for x in repeated)
    counts = {x: repeated.count(x) for x in CLASSES}
    modal = max(CLASSES, key=lambda x: counts[x])
    accepted = modal == 'ACTIVE' and counts['ACTIVE'] >= 8
    return accepted, 'Accepted' if accepted else 'Modal ACTIVE and >=8/10 required', counts['ACTIVE']


def aligned_features(context, target):
    match = re.search(r'Reference completed 1-hour candle:\s*(\d{4}-\d\d-\d\d \d\d:\d\d) UTC', context)
    if not match or int(datetime.strptime(match[1], '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp()) != target-3600:
        raise ValueError('Snapshot reference does not align with target')
    f = extract(context)
    if f['reference'] <= 0:
        raise ValueError('Invalid reference close')
    return f


def training_rows(c):
    rows = {}
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'predictions' in tables:
        for row in c.execute("SELECT * FROM predictions WHERE phase IN ('phase2','phase3') AND actual_direction IN ('UP','DOWN') AND evaluated_at IS NOT NULL ORDER BY id"):
            try:
                f = aligned_features(row['context'], row['target_candle_time'])
                at = datetime.fromisoformat(row['evaluated_at']).timestamp()
                rows.setdefault(row['target_candle_time'], dict(id='legacy:'+str(row['id']), target=row['target_candle_time'],
                    available_at=at, outcome=row['actual_direction'], features=f, snapshot_hash=digest(row['context'])))
            except (ValueError, TypeError, IndexError, KeyError):
                continue
    for row in c.execute("SELECT * FROM phase6_events WHERE actual_direction IN ('UP','DOWN') AND features_json IS NOT NULL"):
        rows[row['target']] = dict(id='phase6:'+str(row['target']), target=row['target'],
            available_at=datetime.fromisoformat(row['evaluated_at']).timestamp(), outcome=row['actual_direction'],
            features=json.loads(row['features_json']), snapshot_hash=row['snapshot_hash'])
    return list(rows.values())


def cached_calls(c, source):
    """Only same target, byte-identical input, frozen config and requested model.

    A historical 4R result is never reused for a different live target. Only ten
    fully valid slots count; cache lookup never writes to historical tables.
    """
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'volatility_predictions' not in tables:
        return {}
    original = c.execute('''SELECT p.*,cfg.config_json FROM volatility_predictions p
        JOIN phase4_configs cfg USING(config_id) WHERE target_candle_time=? AND predictor='openai' ''', (source['target_candle_time'],)).fetchone()
    if original is None or original['context'] != source['context'] or original['model_version'] != source['model_version'] or json.loads(original['config_json']) != json.loads(source['config_json']):
        return {}
    cache = {0: (probabilities([original['p_quiet'],original['p_normal'],original['p_active']]), original['model_version'], json.dumps(dict(original)), 'volatility_predictions:'+str(original['id']))}
    if 'phase4r_sources' not in tables or 'phase4r_runs' not in tables:
        return cache
    saved = c.execute('SELECT source_json FROM phase4r_sources WHERE source_id=?', (original['id'],)).fetchone()
    if saved is None:
        return cache
    frozen = json.loads(saved[0])
    if (any(frozen.get(k) != source[k] for k in ('context','model_version','target_candle_time'))
            or json.loads(frozen['config_json']) != json.loads(source['config_json'])):
        return cache
    repeated = c.execute("SELECT * FROM phase4r_runs WHERE source_id=? AND status='complete' ORDER BY repeat_no", (original['id'],)).fetchall()
    if len(repeated) == 10:
        for r in repeated:
            p = probabilities([r['p_quiet'],r['p_normal'],r['p_active']])
            cache[r['repeat_no']] = (p, r['model_version'], r['raw_json'], f"phase4r_runs:{original['id']}:{r['repeat_no']}")
    return cache


def volatility_slot(target, slot, source, call, cached=None):
    with database() as c:
        c.execute('INSERT INTO phase6_calls(target,slot,status,started_at) VALUES (?,?,?,?)', (target,slot,'started',now()))
    try:
        p, model, raw, ref = (*call(source), None) if cached is None else cached
        p = probabilities(p)
        with database() as c:
            c.execute("UPDATE phase6_calls SET status='complete',completed_at=?,class=?,probabilities_json=?,model_version=?,raw_json=?,cache_ref=? WHERE target=? AND slot=?",
                      (now(),winner(p),json.dumps(p),model,raw,ref,target,slot))
        return winner(p)
    except Exception as exc:
        with database() as c:
            c.execute("UPDATE phase6_calls SET status='failed',completed_at=?,error=?,model_version=?,raw_json=? WHERE target=? AND slot=?",
                      (now(),str(exc),getattr(exc,'model_version',None),getattr(exc,'raw_json',None),target,slot))
        return None


def direction_call(context, model):
    from openai import OpenAI
    prompt = ('Predict direction of BTC-USD over the next completed 1h candle. Use ONLY supplied '
        'Phase 3 features; no news, external knowledge or data after the cutoff. UP means target close '
        '> reference close; DOWN means target close < reference close. Imbalance 0.5 is balanced; '
        'buy_ratio >0.5 means aggressive buyers; microprice offset is microprice minus midpoint. '
        'Estimate P(UP), P(DOWN). Return JSON only: {"p_up":0.5,"p_down":0.5,"reason":"short"}.\nMARKET DATA:\n')
    response = OpenAI(max_retries=0, timeout=120).responses.create(model=model, input=prompt+context.replace(
        'Realized volatility class of the next completed 1-hour candle.', 'Direction of the next completed 1-hour candle.'))
    raw = response.model_dump_json()
    try:
        text = response.output_text.strip()
        if text.startswith('```'):
            text = '\n'.join(text.splitlines()[1:-1])
        value = json.loads(text)
        result = output(p_up=value['p_up'], reason=str(value.get('reason','')))
        down = value['p_down']
        if isinstance(down,bool) or not isinstance(down,(int,float)) or not math.isfinite(down) or not 0 <= down <= 1 or not math.isclose(value['p_up']+down,1,abs_tol=1e-6):
            raise ValueError('Invalid direction probabilities')
        result['p_down'] = down
    except Exception as exc:
        result = output(reason='Invalid provider response: '+str(exc))
    result.update(model_version=getattr(response,'model',model), raw_json=raw)
    return result


def save_method(target, method, result):
    with database() as c:
        c.execute('''UPDATE phase6_predictions SET status=?,completed_at=?,direction=?,p_up=?,p_down=?,version=?,
           model_version=?,raw_json=?,reason=?,audit_json=? WHERE target=? AND method=? AND status='started' ''',
           (result['status'],now(),result['direction'],result['p_up'],result['p_down'],result['version'],
            result.get('model_version'),result.get('raw_json'),result['reason'],json.dumps(result.get('audit')),target,method))


def run(target, clock=None, builder=None, class_call=None, gpt_call=None):
    from phase4_runner import build_snapshot
    from next_stages import class_call as repeated_call
    builder = builder or build_snapshot
    class_call = class_call or repeated_call
    gpt_call = gpt_call or direction_call
    clock_fn = time.time if clock is None else lambda: clock
    with database() as c:
        state = c.execute('SELECT * FROM phase6_config').fetchone()
        if state is None:
            return
        cfg = json.loads(state['config_json'])
        if {k:v for k,v in cfg.items() if k not in ('historical_frequency','implementation_hashes')} != design(cfg['gate']['volatility_config'],cfg['gate']['model']):
            raise RuntimeError('Frozen Phase 6 design differs from code')
        if cfg.get('implementation_hashes') != implementation_hashes():
            raise RuntimeError('Frozen Phase 6 implementation hashes differ from deployed code')
        if config_hash(cfg) != state['config_hash']:
            raise RuntimeError('Frozen Phase 6 configuration hash mismatch')
        # Caller owns the cross-process flock: prior unfinished reservations were interrupted.
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE phase6_predictions SET status='unavailable',reason='Interrupted attempt; no retry',completed_at=? WHERE status='started'", (now(),))
        c.execute("UPDATE phase6_events SET gate='rejected',reason='Interrupted gate; no retry' WHERE gate='pending'")
        if target < state['start_target'] or target % 3600 or not 0 <= clock_fn()-target <= cfg['deadline_seconds']:
            return
        if c.execute("SELECT count(*) FROM phase6_events WHERE gate='accepted'").fetchone()[0] >= cfg['stop']['accepted_gates']:
            return
        if c.execute('SELECT 1 FROM phase6_events WHERE target=?', (target,)).fetchone():
            return
        c.execute("INSERT INTO phase6_events(target,started_at,gate) VALUES (?,?,'pending')", (target,now()))
    try:
        # Reuse an existing single-shot snapshot, if available; never reconstruct it.
        with database() as c:
            row = c.execute("SELECT context FROM volatility_predictions WHERE target_candle_time=? AND predictor='openai'", (target,)).fetchone()
        context = row[0] if row else builder(target)[0]
        f = aligned_features(context, target)
        with database() as c:
            c.execute('UPDATE phase6_events SET context=?,snapshot_hash=?,features_json=?,reference_close=? WHERE target=?',
                      (context,digest(context),json.dumps(f,sort_keys=True),f['reference'],target))
            training = training_rows(c)
        source = dict(context=context, model_version=cfg['gate']['model'], predictor='openai',
                      config_json=json.dumps(cfg['gate']['volatility_config']), target_candle_time=target,
                      timeout=cfg['provider_timeout_seconds'])
        with database() as c:
            cache = cached_calls(c, source)
        if clock_fn()-target > cfg['deadline_seconds']:
            raise RuntimeError('Snapshot acquisition exceeded live deadline')
        single = volatility_slot(target, 0, source, class_call, cache.get(0))
        repeated = []
        if single == 'ACTIVE' and clock_fn()-target <= cfg['deadline_seconds']:
            with ThreadPoolExecutor(max_workers=cfg['repeat_workers']) as pool:
                repeated = list(pool.map(lambda slot: volatility_slot(target,slot,source,class_call,cache.get(slot)), range(1,11)))
        accepted, reason, votes = gate(single, repeated)
        if clock_fn()-target > cfg['deadline_seconds']:
            accepted, reason = False, 'Gate completed after live deadline; NO SIGNAL'
        metadata = dict(single=single, repeats=repeated, active_votes=votes,
                        config_hash=state['config_hash'], snapshot_hash=digest(context))
        with database() as c:
            c.execute('UPDATE phase6_events SET gate=?,reason=?,single_class=?,active_votes=?,gate_json=? WHERE target=?',
                      ('accepted' if accepted else 'rejected',reason,single,votes,json.dumps(metadata),target))
            if accepted:
                c.executemany('INSERT INTO phase6_predictions(target,method,status,started_at,version) VALUES (?, ?, ?, ?, ?)',
                              [(target,m,'started',now(),VERSION) for m in METHODS])
        if not accepted:
            return
        def method_call(method):
            try:
                if clock_fn()-target > cfg['deadline_seconds']:
                    return output(reason='Gate finished after live direction deadline; no backfill')
                if method == 'gpt':
                    return gpt_call(context,cfg['gate']['model'])
                if method == 'logistic':
                    return logistic(f,training,target)
                return rule(method,f)
            except Exception as exc:
                return output(reason='Method failed: '+str(exc))
        def run_method(method):
            result = method_call(method)
            save_method(target,method,result)
            return result
        with ThreadPoolExecutor(max_workers=len(BASE_METHODS)) as pool:
            results = dict(zip(BASE_METHODS,pool.map(run_method,BASE_METHODS)))
        for method,result in ensembles(results).items():
            save_method(target,method,result)
    except Exception as exc:
        with database() as c:
            c.execute("UPDATE phase6_events SET gate='rejected',reason=? WHERE target=? AND gate='pending'", (str(exc),target))
            c.execute("UPDATE phase6_predictions SET status='unavailable',reason=?,completed_at=? WHERE target=? AND status='started'", (str(exc),now(),target))


def evaluate(fetch=None, clock=None):
    from evaluate import get_target_candle
    fetch = fetch or get_target_candle
    clock = time.time() if clock is None else clock
    with database() as c:
        rows = c.execute('SELECT * FROM phase6_events WHERE reference_close IS NOT NULL AND evaluated_at IS NULL AND target+3600<=?', (clock,)).fetchall()
    for row in rows:
        try:
            candle = fetch(row['target'])
            if candle is None or int(candle[0]) != row['target']:
                continue
            close = float(candle[4])
            if not math.isfinite(close) or close <= 0:
                continue
            ret = 100*(close/row['reference_close']-1)
            actual = 'UP' if ret > 0 else 'DOWN' if ret < 0 else 'FLAT'
            with database() as c:
                c.execute('BEGIN IMMEDIATE')
                if c.execute('SELECT evaluated_at FROM phase6_events WHERE target=?', (row['target'],)).fetchone()[0]:
                    continue
                c.execute('UPDATE phase6_events SET actual_return=?,actual_direction=?,evaluated_at=?,target_raw_json=? WHERE target=?',
                          (ret,actual,now(),json.dumps(candle),row['target']))
                for p in c.execute("SELECT * FROM phase6_predictions WHERE target=? AND status='complete'", (row['target'],)).fetchall():
                    correct = None if actual=='FLAT' else int(p['direction']==actual)
                    brier = None if actual=='FLAT' or p['p_up'] is None else (p['p_up']-int(actual=='UP'))**2
                    c.execute('UPDATE phase6_predictions SET correct=?,brier=?,signed_pnl=? WHERE target=? AND method=?',
                              (correct,brier,ret*(1 if p['direction']=='UP' else -1),row['target'],p['method']))
        except Exception as exc:
            print('Phase 6 evaluation pending:', row['target'], str(exc), flush=True)


def cycle():
    """Invoked alongside Phase 5 under the scheduler's existing exclusive lock."""
    from predict_gpt import get_prediction_cutoff
    migrate()
    # Prediction first; historical evaluations fetched after reservation cannot enter training.
    run(get_prediction_cutoff())
    evaluate()


def main():
    import sys
    with open(ROOT/'.next-stages.lock','a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if sys.argv[1:] == ['--migrate']:
            migrate()
        elif sys.argv[1:] == ['--frequency']:
            with sqlite3.connect(DB_PATH.resolve().as_uri()+'?mode=ro',uri=True) as c:
                c.row_factory = sqlite3.Row
                print(json.dumps(historical_frequency(c),indent=2,sort_keys=True))
        elif sys.argv[1:] == ['--start']:
            start()
            with database() as c:
                print(json.dumps(dict(c.execute('SELECT * FROM phase6_config').fetchone()),indent=2,sort_keys=True))
        else:
            raise SystemExit('Use --migrate, --frequency or --start. Live runs belong to next_stages.py.')


if __name__ == '__main__':
    main()
