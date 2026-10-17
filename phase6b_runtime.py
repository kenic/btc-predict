"""Phase6 storage-isolated runtime copy; see PHASE6B.md and parity tests.

Only table namespace, validation and the additional observation limit differ.
Shared prediction helpers remain imported from the original frozen Phase6.
"""
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from phase4 import now, config_hash
from volatility import probabilities, winner
from phase6 import (design, implementation_hashes, aligned_features, cached_calls,
                    direction_call, digest, gate)
from phase6_methods import BASE_METHODS, METHODS, VERSION, output, rule, logistic, ensembles
from phase6b import database, validate, ADDITIONAL


def training_rows(c):
    from phase6 import training_rows as original
    rows = {r['target']: r for r in original(c)}
    for row in c.execute("SELECT * FROM phase6b_events WHERE actual_direction IN ('UP','DOWN') AND features_json IS NOT NULL"):
        from datetime import datetime
        rows[row['target']] = dict(id='phase6b:'+str(row['target']), target=row['target'],
            available_at=datetime.fromisoformat(row['evaluated_at']).timestamp(),
            outcome=row['actual_direction'], features=json.loads(row['features_json']),
            snapshot_hash=row['snapshot_hash'])
    return list(rows.values())


def volatility_slot(target, slot, source, call, cached=None):
    with database() as c:
        c.execute('INSERT INTO phase6b_calls(target,slot,status,started_at) VALUES (?,?,?,?)', (target,slot,'started',now()))
    try:
        p, model, raw, ref = (*call(source), None) if cached is None else cached
        p = probabilities(p)
        with database() as c:
            c.execute("UPDATE phase6b_calls SET status='complete',completed_at=?,class=?,probabilities_json=?,model_version=?,raw_json=?,cache_ref=? WHERE target=? AND slot=?",
                      (now(),winner(p),json.dumps(p),model,raw,ref,target,slot))
        return winner(p)
    except Exception as exc:
        with database() as c:
            c.execute("UPDATE phase6b_calls SET status='failed',completed_at=?,error=?,model_version=?,raw_json=? WHERE target=? AND slot=?",
                      (now(),str(exc),getattr(exc,'model_version',None),getattr(exc,'raw_json',None),target,slot))
        return None


def save_method(target, method, result):
    with database() as c:
        c.execute('''UPDATE phase6b_predictions SET status=?,completed_at=?,direction=?,p_up=?,p_down=?,version=?,
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
        validate(c)
        state = c.execute('SELECT * FROM phase6b_config').fetchone()
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
        c.execute("UPDATE phase6b_predictions SET status='unavailable',reason='Interrupted attempt; no retry',completed_at=? WHERE status='started'", (now(),))
        c.execute("UPDATE phase6b_events SET gate='rejected',reason='Interrupted gate; no retry' WHERE gate='pending'")
        if target < state['start_target'] or target % 3600 or not 0 <= clock_fn()-target <= cfg['deadline_seconds']:
            return
        if c.execute("SELECT count(*) FROM phase6b_events WHERE gate='accepted'").fetchone()[0] >= ADDITIONAL:
            return
        if c.execute('SELECT 1 FROM phase6b_events WHERE target=?', (target,)).fetchone():
            return
        c.execute("INSERT INTO phase6b_events(target,started_at,gate) VALUES (?,?,'pending')", (target,now()))
    try:
        # Reuse an existing single-shot snapshot, if available; never reconstruct it.
        with database() as c:
            row = c.execute("SELECT context FROM volatility_predictions WHERE target_candle_time=? AND predictor='openai'", (target,)).fetchone()
        context = row[0] if row else builder(target)[0]
        f = aligned_features(context, target)
        with database() as c:
            c.execute('UPDATE phase6b_events SET context=?,snapshot_hash=?,features_json=?,reference_close=? WHERE target=?',
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
            c.execute('UPDATE phase6b_events SET gate=?,reason=?,single_class=?,active_votes=?,gate_json=? WHERE target=?',
                      ('accepted' if accepted else 'rejected',reason,single,votes,json.dumps(metadata),target))
            if accepted:
                c.executemany('INSERT INTO phase6b_predictions(target,method,status,started_at,version) VALUES (?, ?, ?, ?, ?)',
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
                return rule(method,f,target=target)
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
            c.execute("UPDATE phase6b_events SET gate='rejected',reason=? WHERE target=? AND gate='pending'", (str(exc),target))
            c.execute("UPDATE phase6b_predictions SET status='unavailable',reason=?,completed_at=? WHERE target=? AND status='started'", (str(exc),now(),target))


def evaluate(fetch=None, clock=None):
    from evaluate import get_target_candle
    fetch = fetch or get_target_candle
    clock = time.time() if clock is None else clock
    with database() as c:
        rows = c.execute('SELECT * FROM phase6b_events WHERE reference_close IS NOT NULL AND evaluated_at IS NULL AND target+3600<=?', (clock,)).fetchall()
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
                if c.execute('SELECT evaluated_at FROM phase6b_events WHERE target=?', (row['target'],)).fetchone()[0]:
                    continue
                c.execute('UPDATE phase6b_events SET actual_return=?,actual_direction=?,evaluated_at=?,target_raw_json=? WHERE target=?',
                          (ret,actual,now(),json.dumps(candle),row['target']))
                for p in c.execute("SELECT * FROM phase6b_predictions WHERE target=? AND status='complete'", (row['target'],)).fetchall():
                    correct = None if actual=='FLAT' else int(p['direction']==actual)
                    brier = None if actual=='FLAT' or p['p_up'] is None else (p['p_up']-int(actual=='UP'))**2
                    c.execute('UPDATE phase6b_predictions SET correct=?,brier=?,signed_pnl=? WHERE target=? AND method=?',
                              (correct,brier,ret*(1 if p['direction']=='UP' else -1),row['target'],p['method']))
        except Exception as exc:
            print('Phase 6 evaluation pending:', row['target'], str(exc), flush=True)
