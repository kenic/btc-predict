"""Automatic Phase6-to-6b handoff with isolated extended observation."""
import argparse
import fcntl
import json
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from phase4 import DB_PATH, ROOT, config_hash
import phase6

PRIMARY = 96
CUMULATIVE = 200
ADDITIONAL = CUMULATIVE - PRIMARY
FILES = ('phase6b.py', 'phase6b_runtime.py', 'PHASE6B.md',
         'deploy/systemd/btc-phase6b.service', 'deploy/systemd/btc-phase6b.timer')


@contextmanager
def database():
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.row_factory = sqlite3.Row
        yield c


def hashes():
    return {name: phase6.digest((ROOT/name).read_text()) for name in FILES}


def primary_snapshot(c, final=False):
    state = c.execute('SELECT * FROM phase6_config').fetchone()
    if state is None:
        raise RuntimeError('Phase6 has not started')
    cfg = json.loads(state['config_json'])
    expected = phase6.design(cfg['gate']['volatility_config'], cfg['gate']['model'])
    actual = {k:v for k,v in cfg.items() if k not in ('historical_frequency','implementation_hashes')}
    if actual != expected or cfg['stop']['accepted_gates'] != PRIMARY:
        raise RuntimeError('Original Phase6 preregistration differs')
    if config_hash(cfg) != state['config_hash'] or cfg['implementation_hashes'] != phase6.implementation_hashes():
        raise RuntimeError('Original Phase6 frozen hash mismatch')
    events = c.execute("SELECT * FROM phase6_events WHERE gate='accepted' ORDER BY target").fetchall()
    if len(events) != PRIMARY:
        raise RuntimeError('Phase6 must have exactly 96 accepted ACTIVE targets')
    if final and any(r['evaluated_at'] is None for r in events):
        return None
    if c.execute("SELECT 1 FROM phase6_events WHERE gate='pending'").fetchone():
        raise RuntimeError('Unfinished Phase6 gate')
    targets = [r['target'] for r in events]
    tables = {'events': [dict(r) for r in events]}
    for suffix in ('calls','predictions'):
        rows = c.execute(f"SELECT * FROM phase6_{suffix} WHERE target IN (SELECT target FROM phase6_events WHERE gate='accepted') ORDER BY target," + ('slot' if suffix=='calls' else 'method')).fetchall()
        if any(r['status']=='started' for r in rows):
            raise RuntimeError('Unfinished Phase6 calls or predictions')
        if final and suffix == 'predictions' and any(r['status']=='complete' and r['signed_pnl'] is None for r in rows):
            return None
        tables[suffix] = [dict(r) for r in rows]
    if not final:
        # Predictions/membership freeze at96; original evaluator may finish outcomes.
        mutable = {'events': {'actual_return','actual_direction','evaluated_at','target_raw_json'},
                   'predictions': {'correct','brier','signed_pnl'}, 'calls': set()}
        tables = {name: [{k:v for k,v in row.items() if k not in mutable[name]} for row in rows]
                  for name,rows in tables.items()}
    return dict(state), dict(targets=targets, accepted=PRIMARY,
        snapshot_hash=config_hash(tables), config_record_hash=config_hash(dict(state)))


def evaluation_snapshot(c, targets=None):
    """Protect outcomes already complete when the membership is frozen."""
    if targets is None:
        targets = [r[0] for r in c.execute("SELECT target FROM phase6_events WHERE gate='accepted' AND evaluated_at IS NOT NULL ORDER BY target")]
    selected = set(targets)
    data = {table: [dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY target'+(',method' if table.endswith('predictions') else '')) if r['target'] in selected]
            for table in ('phase6_events','phase6_predictions')}
    return dict(targets=targets, snapshot_hash=config_hash(data))


def validate(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='phase6b_manifest'").fetchone():
        raise RuntimeError('Phase6b has not started')
    record = c.execute('SELECT * FROM phase6b_manifest').fetchone()
    if record is None:
        raise RuntimeError('Phase6b has not started')
    manifest = json.loads(record['manifest_json'])
    if manifest.get('version') != 'phase6b-v2':
        raise RuntimeError('Unsupported Phase6b manifest version; preserve existing state')
    if config_hash(manifest) != record['manifest_hash']:
        raise RuntimeError('Phase6b manifest hash mismatch')
    state, closure = primary_snapshot(c)
    if manifest['primary_closure'] != closure or manifest['implementation_hashes'] != hashes():
        raise RuntimeError('Phase6 closure or Phase6b implementation changed')
    if evaluation_snapshot(c, manifest['initial_evaluations']['targets']) != manifest['initial_evaluations']:
        raise RuntimeError('Previously evaluated Phase6 results changed')
    b = c.execute('SELECT * FROM phase6b_config').fetchone()
    if b is None or dict(b) != manifest['runtime_config']:
        raise RuntimeError('Phase6b runtime config changed')
    if b['config_json'] != state['config_json'] or b['config_hash'] != state['config_hash']:
        raise RuntimeError('Phase6b must use the original frozen Phase6 config')
    if manifest['stop'] != {'cumulative_accepted': CUMULATIVE, 'additional_accepted': ADDITIONAL, 'optional_stopping': False}:
        raise RuntimeError('Phase6b stopping rule changed')
    sealed = c.execute('SELECT * FROM phase6b_primary_final').fetchone()
    if sealed is not None:
        final = primary_snapshot(c, final=True)
        if final is None or config_hash(final[1]) != sealed['result_hash']:
            raise RuntimeError('Final Phase6 results changed after sealing')
    return manifest


def seal_primary(c):
    """Append final outcome hash once; original evaluator owns original rows."""
    if c.execute('SELECT 1 FROM phase6b_primary_final').fetchone():
        return
    final = primary_snapshot(c, final=True)
    if final is not None:
        c.execute('INSERT INTO phase6b_primary_final VALUES(1,?,?)',
                  (datetime.now(timezone.utc).isoformat(), config_hash(final[1])))


def start(clock=None):
    """Caller owns shared lock. Freezes membership/predictions; actuals may follow."""
    clock = time.time() if clock is None else clock
    with database() as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='phase6b_manifest'").fetchone():
            if c.execute('SELECT 1 FROM phase6b_manifest').fetchone():
                return validate(c)
        # Validate before creating even the additive schema.
        primary_snapshot(c)
    sha = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    dirty = subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,text=True)
    if dirty.strip():
        raise RuntimeError('Commit implementation before starting Phase6b')
    migrate()
    with database() as c:
        c.execute('BEGIN IMMEDIATE')
        state, closure = primary_snapshot(c)
        boundary = closure['targets'][-1]+3600
        runtime = dict(state, started_at=datetime.fromtimestamp(clock,timezone.utc).isoformat(),
                       start_target=boundary, code_sha=sha)
        manifest = dict(version='phase6b-v2', purpose='Extended replication / observation of Phase 6.',
            change='Observation horizon only', primary_closure=closure,
            initial_evaluations=evaluation_snapshot(c),
            closed_at=runtime['started_at'], runtime_config=runtime,
            implementation_hashes=hashes(),
            stop=dict(cumulative_accepted=CUMULATIVE, additional_accepted=ADDITIONAL, optional_stopping=False))
        c.execute('INSERT INTO phase6b_config VALUES(1,?,?,?,?,?)', tuple(runtime[k] for k in
            ('config_json','config_hash','started_at','start_target','code_sha')))
        c.execute('INSERT INTO phase6b_manifest VALUES(1,?,?)',
                  (json.dumps(manifest,sort_keys=True),config_hash(manifest)))
        seal_primary(c)
        return manifest


def status():
    # Read-only: status never creates a DB, schema, closure or start marker.
    with sqlite3.connect(DB_PATH.resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory = sqlite3.Row
        result = {'phase6_accepted': c.execute("SELECT count(*) FROM phase6_events WHERE gate='accepted'").fetchone()[0],
                  'phase6b_started': False, 'phase6b_accepted': 0, 'phase6_results_sealed': False}
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='phase6b_manifest'").fetchone():
            result['phase6b_started'] = bool(c.execute('SELECT 1 FROM phase6b_manifest').fetchone())
        if result['phase6b_started']:
            validate(c)
            result['phase6_results_sealed'] = bool(c.execute('SELECT 1 FROM phase6b_primary_final').fetchone())
            result['phase6b_accepted'] = c.execute("SELECT count(*) FROM phase6b_events WHERE gate='accepted'").fetchone()[0]
        result['cumulative_accepted'] = result['phase6_accepted'] + result['phase6b_accepted']
        return result


def cycle():
    """Preinstalled timer waits read-only below96, then starts at the next hour.

    Caller owns the original scheduler lock, preventing overlapping 6/6b calls.
    No manual start or evaluation-completion wait is needed at the handoff.
    """
    from predict_gpt import get_prediction_cutoff
    import phase6b_runtime as runtime
    target = get_prediction_cutoff()
    with database() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='phase6_config'").fetchone():
            return
        if not c.execute('SELECT 1 FROM phase6_config').fetchone():
            return
        n, last = c.execute("SELECT count(*),max(target) FROM phase6_events WHERE gate='accepted'").fetchone()
        if n < PRIMARY:
            return
        if n != PRIMARY:
            raise RuntimeError('Phase6 must stop at exactly96')
        if target <= last:
            return
    start()  # Idempotent; freezes membership even if final outcomes are pending.
    # Prediction precedes evaluation, as in the original Phase6.
    runtime.run(target)
    runtime.evaluate()
    with database() as c:
        validate(c)
        seal_primary(c)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','start','cycle'),nargs='?',default='status')
    args = parser.parse_args()
    if args.action == 'status':
        print(json.dumps(status(),indent=2,sort_keys=True)); return
    with open(ROOT/'.next-stages.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if args.action == 'start':
            print(json.dumps(start(),indent=2,sort_keys=True))
        else:
            from dotenv import load_dotenv
            load_dotenv(ROOT/'.env')
            cycle()


def migrate():
    with database() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS phase6b_config (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), config_json TEXT NOT NULL,
          config_hash TEXT NOT NULL, started_at TEXT NOT NULL, start_target INTEGER NOT NULL,
          code_sha TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS phase6b_events (
          target INTEGER PRIMARY KEY, started_at TEXT NOT NULL, context TEXT,
          snapshot_hash TEXT, features_json TEXT, reference_close REAL,
          gate TEXT NOT NULL, reason TEXT, single_class TEXT, active_votes INTEGER,
          gate_json TEXT, actual_return REAL, actual_direction TEXT, evaluated_at TEXT,
          target_raw_json TEXT);
        CREATE TABLE IF NOT EXISTS phase6b_calls (
          target INTEGER NOT NULL, slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 10),
          status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
          class TEXT, probabilities_json TEXT, model_version TEXT, raw_json TEXT,
          error TEXT, cache_ref TEXT, PRIMARY KEY(target,slot));
        CREATE TABLE IF NOT EXISTS phase6b_predictions (
          target INTEGER NOT NULL, method TEXT NOT NULL, status TEXT NOT NULL,
          started_at TEXT NOT NULL, completed_at TEXT, direction TEXT, p_up REAL, p_down REAL,
          version TEXT NOT NULL, model_version TEXT, raw_json TEXT, reason TEXT, audit_json TEXT,
          correct INTEGER, brier REAL, signed_pnl REAL, PRIMARY KEY(target,method));
        ''')
    with database() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS phase6b_primary_final (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            sealed_at TEXT NOT NULL, result_hash TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS phase6b_manifest (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            manifest_json TEXT NOT NULL, manifest_hash TEXT NOT NULL)""")


if __name__ == '__main__':
    # Runtime imports this module; share a single set of database/validation globals.
    import sys
    sys.modules['phase6b'] = sys.modules[__name__]
    main()
