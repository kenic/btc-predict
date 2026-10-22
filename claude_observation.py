"""Isolated, preregistered Claude side observations; original DB is read-only."""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version as package_version
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
END = int(datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp())
DEADLINE = 600
MODEL = 'claude-opus-5-5'
PARAMS = dict(model=MODEL, max_tokens=4096, thinking={'type': 'adaptive'},
              output_config={'effort': 'medium'})
SOURCES = ('claude_observation.py', 'claude_analysis.py', 'phase5b.py', 'phase6.py',
           'phase6b.py', 'phase6b_runtime.py', 'phase6_methods.py', 'phase4_runner.py',
           'next_stages.py', 'phase4.py', 'volatility.py', 'predict_gpt.py',
           'indicators.py', 'microstructure.py', 'db.py', 'candle_store.py', 'requirements.txt')


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db(path, readonly=False):
    path = Path(path).resolve()
    c = sqlite3.connect(path.as_uri()+'?mode=ro' if readonly else path, uri=readonly, timeout=1)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def prompt_literal(filename, function):
    """Read the unchanged GPT prompt constant without importing/executing its runner."""
    tree = ast.parse((ROOT/filename).read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
    assignment = next(n for n in fn.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'prompt' for t in n.targets))
    return ast.literal_eval(assignment.value)


def spec():
    return dict(version='claude-side-v2-opus55', params=PARAMS, api_version='2023-06-01',
                endpoint='https://api.anthropic.com/v1/messages', timeout=120, retries=0,
                prompts={'rv': prompt_literal('next_stages.py', 'regression_call'),
                         'direction': prompt_literal('phase6.py', 'direction_call')},
                start='next UTC hour after init', end_exclusive=END, deadline=DEADLINE,
                repeats=10, primary_slot=0, repeat_slots=list(range(1, 11)), workers=4,
                stop='fixed calendar boundary; no optional stopping; no replacement targets',
                runtime=sys.version,
                packages={name: package_version(name) for name in (
                    "numpy", "pandas", "requests", "openai", "python-dotenv")},
                hashes={f: hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in SOURCES})


def migrate(path):
    with db(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS claude_config (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), started_at TEXT NOT NULL,
          start_target INTEGER NOT NULL, end_exclusive INTEGER NOT NULL,
          config_json TEXT NOT NULL, config_hash TEXT NOT NULL, code_sha TEXT NOT NULL,
          source_db TEXT NOT NULL, candles_db TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS phase5c_predictions (
          target INTEGER PRIMARY KEY CHECK(target%3600=0), status TEXT NOT NULL,
          started_at TEXT NOT NULL, completed_at TEXT, context TEXT, snapshot_hash TEXT,
          snapshot_source TEXT, previous_rv REAL, predicted_rv REAL, model_version TEXT,
          raw_json TEXT, error TEXT, api_started_at TEXT, actual_rv REAL, evaluated_at TEXT);
        CREATE TABLE IF NOT EXISTS phase6c_events (
          cohort TEXT NOT NULL CHECK(cohort IN ('phase6','phase6b')), target INTEGER NOT NULL,
          context TEXT NOT NULL, snapshot_hash TEXT NOT NULL, source_gate_json TEXT,
          actual_direction TEXT, actual_return REAL, evaluated_at TEXT,
          PRIMARY KEY(cohort,target));
        CREATE TABLE IF NOT EXISTS phase6c_calls (
          cohort TEXT NOT NULL, target INTEGER NOT NULL, slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 10),
          status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, api_started_at TEXT,
          direction TEXT, p_up REAL, p_down REAL, model_version TEXT, raw_json TEXT, error TEXT,
          correct INTEGER, brier REAL, signed_pnl REAL,
          PRIMARY KEY(cohort,target,slot));
        ''')


def initialize(path, source, candles, clock=None):
    if Path(path).resolve() in (Path(source).resolve(), Path(candles).resolve()):
        raise ValueError('Side database must differ from existing databases')
    clock = time.time() if clock is None else clock
    migrate(path)
    with db(path) as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM claude_config').fetchone()
        if row:
            check(row, source, candles)
            return dict(row)
        start = (int(clock)//3600+1)*3600
        if start >= END:
            raise ValueError('Observation window has expired')
        config = json.dumps(spec(), sort_keys=True)
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        c.execute('INSERT INTO claude_config VALUES(1,?,?,?,?,?,?,?,?)',
                  (stamp(), start, END, config, digest(config), sha,
                   str(Path(source).resolve()), str(Path(candles).resolve())))
        return dict(c.execute('SELECT * FROM claude_config').fetchone())


def refreeze_before_start(path, source, candles, clock=None):
    """Explicit authorized model change, only before the first target with zero rows."""
    clock = time.time() if clock is None else clock
    with db(path) as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM claude_config WHERE singleton=1').fetchone()
        if row is None:
            raise RuntimeError('Existing registration required')
        old = dict(row)
        if (clock >= old['start_target'] or old['end_exclusive'] != END or
                old['source_db'] != str(Path(source).resolve()) or
                old['candles_db'] != str(Path(candles).resolve()) or
                digest(old['config_json']) != old['config_hash']):
            raise RuntimeError('Pre-observation registration boundary/integrity check failed')
        for table in ('phase5c_predictions', 'phase6c_events', 'phase6c_calls'):
            if c.execute('SELECT count(*) FROM '+table).fetchone()[0]:
                raise RuntimeError('Observation rows exist; cannot replace registration')
        if json.loads(old['config_json']) == spec():
            check(row, source, candles)
            return old
        previous = json.loads(old['config_json'])
        if previous.get('params', {}).get('model') != 'claude-sonnet-4-6' or previous.get('version') != 'claude-side-v1':
            raise RuntimeError('Only the unused initial Sonnet registration may be replaced')
        config = json.dumps(spec(), sort_keys=True)
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        c.execute('''CREATE TABLE IF NOT EXISTS claude_config_history (
            id INTEGER PRIMARY KEY, replaced_at TEXT NOT NULL, reason TEXT NOT NULL,
            old_registration_json TEXT NOT NULL, new_config_hash TEXT NOT NULL)''')
        c.execute('INSERT INTO claude_config_history(replaced_at,reason,old_registration_json,new_config_hash) VALUES(?,?,?,?)',
                  (stamp(), 'User-authorized Opus 5.5 selection before first target; zero observation rows',
                   json.dumps(old, sort_keys=True), digest(config)))
        c.execute('UPDATE claude_config SET started_at=?,config_json=?,config_hash=?,code_sha=? WHERE singleton=1',
                  (stamp(), config, digest(config), sha))
        result = c.execute('SELECT * FROM claude_config WHERE singleton=1').fetchone()
        check(result, source, candles)
        return dict(result)


def check(row, source, candles):
    if row is None:
        raise RuntimeError('Explicit init required')
    if (digest(row['config_json']) != row['config_hash'] or json.loads(row['config_json']) != spec()
            or row['end_exclusive'] != END or row['start_target'] % 3600
            or row['start_target'] >= END
            or row['source_db'] != str(Path(source).resolve())
            or row['candles_db'] != str(Path(candles).resolve())):
        raise RuntimeError('Frozen configuration/source mismatch; restore registered version')


def request_text(kind, context, cfg):
    if kind == 'direction':
        context = context.replace('Realized volatility class of the next completed 1-hour candle.',
                                  'Direction of the next completed 1-hour candle.')
    return cfg['prompts'][kind]+context


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def provider(kind, context, cfg, timeout):
    """No SDK retries, redirects, external tools, environment config overrides or key logging."""
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        raise RuntimeError('ANTHROPIC_API_KEY missing')
    payload = dict(cfg['params'], messages=[{'role': 'user', 'content': request_text(kind, context, cfg)}])
    req = urllib.request.Request(cfg['endpoint'], data=json.dumps(payload).encode(), method='POST',
                                 headers={'x-api-key': key, 'anthropic-version': cfg['api_version'],
                                          'content-type': 'application/json'})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
        raw = response.read().decode()
    # A provider response must not accidentally persist a credential either.
    return json.loads(raw.replace(key, '[REDACTED]'))


def parse(kind, raw):
    if raw.get('stop_reason') != 'end_turn':
        raise ValueError('Incomplete provider response')
    text = ''.join(b['text'] for b in raw['content'] if b['type'] == 'text').strip()
    if kind == 'direction' and text.startswith('```'):
        text = '\n'.join(text.splitlines()[1:-1])
    value = json.loads(text)
    def number(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError('Invalid finite number')
        return v
    if kind == 'rv':
        rv = number(value['predicted_rv'])
        if rv < 0:
            raise ValueError('Negative RV')
        return dict(predicted_rv=rv)
    up, down = number(value['p_up']), number(value['p_down'])
    if not 0 <= up <= 1 or not 0 <= down <= 1 or not math.isclose(up+down, 1, abs_tol=1e-6):
        raise ValueError('Invalid probabilities')
    return dict(direction='UP' if up >= .5 else 'DOWN', p_up=up, p_down=down)


def update(path, table, keys, **values):
    with db(path) as c:
        c.execute(f'UPDATE {table} SET '+','.join(k+'=?' for k in values)+' WHERE '+
                  ' AND '.join(k+'=?' for k in keys), tuple(values.values())+tuple(keys.values()))


def invoke(path, table, keys, kind, context, cfg, clock_fn, call):
    target = keys['target']
    raw = None
    try:
        remaining = min(target+DEADLINE, END)-clock_fn()
        if remaining <= 0 or clock_fn() < target:
            update(path, table, keys, status='missed', completed_at=stamp(), error='Live deadline missed')
            return
        if call is provider and not os.environ.get('ANTHROPIC_API_KEY'):
            raise RuntimeError('Credential unavailable')
        update(path, table, keys, api_started_at=stamp())
        raw = call(kind, context, cfg, min(cfg['timeout'], remaining))
        result = parse(kind, raw)
        ontime = target <= clock_fn() < min(target+DEADLINE, END)
        update(path, table, keys, status='complete' if ontime else 'late', completed_at=stamp(),
               model_version=raw.get('model'), raw_json=json.dumps(raw), **result)
    except Exception as exc:
        # Never save exception text: HTTP errors may contain headers, credentials or bodies.
        update(path, table, keys, status='failed', completed_at=stamp(), error=type(exc).__name__,
               raw_json=json.dumps(raw) if raw is not None else None)


def has_table(c, name):
    return c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (name,)).fetchone() is not None


def observe_rv(path, source, state, cfg, clock_fn, call, builder):
    clock = clock_fn()
    target = int(clock)//3600*3600
    with db(path) as c:
        for t in range(state['start_target'], min(target, END-3600)+1, 3600):
            c.execute('INSERT OR IGNORE INTO phase5c_predictions(target,status,started_at) VALUES(?,?,?)',
                      (t, 'waiting' if t == target and clock < t+DEADLINE else 'missed', stamp()))
        c.execute("UPDATE phase5c_predictions SET status='missed',completed_at=? WHERE status='waiting' AND target+?<=?",
                  (stamp(), DEADLINE, clock))
        row = c.execute('SELECT * FROM phase5c_predictions WHERE target=?', (target,)).fetchone()
    if not row or row['status'] != 'waiting' or clock >= END:
        return
    with db(source, True) as c:
        original = c.execute('SELECT * FROM phase5b_predictions WHERE target_candle_time=?', (target,)).fetchone()
        registration = c.execute('SELECT * FROM phase5b_config').fetchone()
    # Within 5b's registered window wait for its exact saved input, including on failures.
    overlap = registration and registration['start_target'] <= target <= registration['end_target']
    if overlap and (not original or not original['context']):
        return
    with db(path) as c:
        claimed = c.execute("UPDATE phase5c_predictions SET status='started' WHERE target=? AND status='waiting'", (target,)).rowcount
    if not claimed:
        return
    try:
        if original and original['context']:
            context, previous, origin = original['context'], original['previous_rv'], 'phase5b'
            if digest(context) != original['snapshot_hash']:
                raise ValueError('Source snapshot hash mismatch')
        else:
            if builder is None:
                from phase4_runner import build_snapshot
                builder = build_snapshot
            context, previous = builder(target)
            origin = 'phase5b_builder_outside_5b_window'
        update(path, 'phase5c_predictions', {'target': target}, context=context,
               snapshot_hash=digest(context), previous_rv=previous, snapshot_source=origin)
        invoke(path, 'phase5c_predictions', {'target': target}, 'rv', context, cfg, clock_fn, call)
    except Exception as exc:
        update(path, 'phase5c_predictions', {'target': target}, status='failed', completed_at=stamp(), error=type(exc).__name__)


def observe_direction(path, source, state, cfg, clock_fn, call):
    clock = clock_fn()
    for cohort in ('phase6', 'phase6b'):
        with db(source, True) as c:
            if not has_table(c, cohort+'_events'):
                continue
            events = [dict(r) for r in c.execute(f'SELECT * FROM {cohort}_events WHERE gate="accepted" AND target>=? AND target<?',
                                               (state['start_target'], min(END, int(clock)//3600*3600+1)))]
        jobs = []
        for event in events:
            target, context = event['target'], event['context']
            if not context or digest(context) != event['snapshot_hash']:
                raise ValueError('Accepted source snapshot invalid')
            with db(path) as c:
                c.execute('INSERT OR IGNORE INTO phase6c_events(cohort,target,context,snapshot_hash,source_gate_json) VALUES(?,?,?,?,?)',
                          (cohort, target, context, event['snapshot_hash'], event['gate_json']))
                saved = c.execute('SELECT * FROM phase6c_events WHERE cohort=? AND target=?', (cohort, target)).fetchone()
                if saved['context'] != context or saved['snapshot_hash'] != event['snapshot_hash']:
                    raise ValueError('Source changed after observation')
                for slot in range(11):
                    eligible = target <= clock_fn() < min(target+DEADLINE, END)
                    claimed = c.execute('INSERT OR IGNORE INTO phase6c_calls(cohort,target,slot,status,started_at) VALUES(?,?,?,?,?)',
                                        (cohort, target, slot, 'started' if eligible else 'missed', stamp())).rowcount
                    if claimed and eligible:
                        jobs.append((dict(cohort=cohort, target=target, slot=slot), context))
        # Primary slot first; all repeats use identical frozen requests, never any gate.
        with ThreadPoolExecutor(max_workers=cfg['workers']) as pool:
            list(pool.map(lambda job: invoke(path, 'phase6c_calls', job[0], 'direction', job[1], cfg, clock_fn, call), jobs))


def evaluate(path, source, candles, clock):
    from candle_store import read_window
    from volatility import realized_volatility
    with db(path) as c:
        rv_rows = c.execute('SELECT target FROM phase5c_predictions WHERE evaluated_at IS NULL AND target+3660<=?', (clock,)).fetchall()
        events = c.execute('SELECT * FROM phase6c_events WHERE evaluated_at IS NULL').fetchall()
    for row in rv_rows:
        try:
            rv = realized_volatility(list(read_window(candles, row['target']).values()), row['target'])
            update(path, 'phase5c_predictions', {'target': row['target']}, actual_rv=rv, evaluated_at=stamp())
        except (ValueError, sqlite3.Error, OSError):
            continue
    for event in events:
        with db(source, True) as c:
            original = c.execute(f'SELECT * FROM {event["cohort"]}_events WHERE target=? AND gate="accepted"', (event['target'],)).fetchone()
        if not original or not original['evaluated_at'] or event['target']+3600 > clock:
            continue
        if original['snapshot_hash'] != event['snapshot_hash']:
            raise ValueError('Evaluation snapshot mismatch')
        actual, ret = original['actual_direction'], original['actual_return']
        if actual not in ('UP', 'DOWN', 'FLAT') or not isinstance(ret, (int, float)) or not math.isfinite(ret):
            continue
        with db(path) as c:
            c.execute('UPDATE phase6c_events SET actual_direction=?,actual_return=?,evaluated_at=? WHERE cohort=? AND target=?',
                      (actual, ret, original['evaluated_at'], event['cohort'], event['target']))
            for p in c.execute('SELECT * FROM phase6c_calls WHERE cohort=? AND target=? AND status="complete"', (event['cohort'], event['target'])).fetchall():
                c.execute('UPDATE phase6c_calls SET correct=?,brier=?,signed_pnl=? WHERE cohort=? AND target=? AND slot=?',
                          (None if actual == 'FLAT' else int(p['direction'] == actual),
                           None if actual == 'FLAT' else (p['p_up']-int(actual == 'UP'))**2,
                           ret*(1 if p['direction'] == 'UP' else -1), event['cohort'], event['target'], p['slot']))


def cycle(path, source, candles, clock_fn=time.time, call=provider, builder=None):
    with db(path) as c:
        state = dict(c.execute('SELECT * FROM claude_config').fetchone())
    check(state, source, candles)
    cfg = json.loads(state['config_json'])
    errors = []
    # Independent branches: RV API failure/input error cannot suppress accepted direction observation.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(observe_rv, path, source, state, cfg, clock_fn, call, builder),
                   pool.submit(observe_direction, path, source, state, cfg, clock_fn, call)]
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                errors.append(type(exc).__name__)
    evaluate(path, source, candles, clock_fn())
    if errors:
        raise RuntimeError('Side observation errors: '+','.join(errors))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('init', 'refreeze-before-start', 'cycle', 'status'))
    p.add_argument('--db', type=Path, default=ROOT/'claude-observation.db')
    p.add_argument('--source-db', type=Path, default=ROOT/'btc.db')
    p.add_argument('--candles-db', type=Path, default=ROOT/'candles.db')
    a = p.parse_args()
    if a.action == 'init':
        print(json.dumps(initialize(a.db, a.source_db, a.candles_db), indent=2))
    elif a.action == 'refreeze-before-start':
        print(json.dumps(refreeze_before_start(a.db, a.source_db, a.candles_db), indent=2))
    elif a.action == 'cycle':
        cycle(a.db, a.source_db, a.candles_db)
    else:
        with db(a.db, True) as c:
            print(dict(c.execute('SELECT start_target,end_exclusive,code_sha,config_hash FROM claude_config').fetchone()))
            for table in ('phase5c_predictions', 'phase6c_calls'):
                print(table, [dict(r) for r in c.execute(f'SELECT status,count(*) AS n,sum(api_started_at IS NOT NULL) AS attempted FROM {table} GROUP BY status')])


if __name__ == '__main__':
    main()
