"""Independent, fixed-window Phase 5b observation. Never calls Phase 6 writers."""
import argparse
from contextlib import contextmanager
import hashlib
import inspect
from importlib.metadata import version as package_version
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
VERSION = 'phase5b-v1'
LIMIT = 500
DEADLINE = 600
FROZEN_FILES = ('phase5b.py', 'next_stages.py', 'phase4_runner.py', 'phase4.py',
                'volatility.py', 'predict_gpt.py', 'indicators.py', 'microstructure.py',
                'db.py', 'candle_store.py', 'requirements.txt')


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@contextmanager
def database(path):
    c = sqlite3.connect(path, timeout=1)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def migrate(path):
    """Only additive 5b DDL; no existing migrations or journal mode changes."""
    with database(path) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS phase5b_config (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            started_at TEXT NOT NULL, start_target INTEGER NOT NULL CHECK(start_target%3600=0),
            end_target INTEGER NOT NULL, version TEXT NOT NULL, code_sha TEXT NOT NULL,
            config_json TEXT NOT NULL, config_hash TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS phase5b_predictions (
            target_candle_time INTEGER PRIMARY KEY CHECK(target_candle_time%3600=0),
            started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
            previous_rv REAL, predicted_rv REAL, version TEXT NOT NULL,
            model_version TEXT, snapshot_hash TEXT, context TEXT, raw_json TEXT,
            error TEXT, actual_rv REAL, evaluated_at TEXT)''')


def specification():
    from next_stages import regression_call
    from predict_gpt import MODEL
    return dict(version=VERSION, targets=LIMIT, deadline_seconds=DEADLINE,
                requested_model=MODEL, predictor_source=inspect.getsource(regression_call),
                runtime=dict(python=sys.version, packages={name: package_version(name)
                             for name in ('openai', 'numpy', 'pandas', 'requests', 'python-dotenv')}),
                rv='100*sqrt(sum(12 consecutive 5m log returns squared)); 13 closes; percent; nonannualized',
                stop='500 consecutive hourly targets; failures/missed count; no optional stopping',
                retries='none; started is uncertain; no historical model calls',
                actual_source='read-only candles.db; missing stays pending; no interpolation',
                implementation_hashes={name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                                       for name in FROZEN_FILES})


def initialize(path, clock=None):
    """Explicit preregistration, future-only boundary. Repeated init cannot reset it."""
    clock = time.time() if clock is None else clock
    migrate(path)
    config = json.dumps(specification(), sort_keys=True)
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    start = (int(clock)//3600+1)*3600
    with database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute('SELECT * FROM phase5b_config').fetchone()
        if existing:
            checked_config(existing)
            return dict(existing)
        c.execute('INSERT INTO phase5b_config VALUES(1,?,?,?,?,?,?,?)',
                  (now(), start, start+(LIMIT-1)*3600, VERSION, sha, config, digest(config)))
        return dict(c.execute('SELECT * FROM phase5b_config').fetchone())


def checked_config(row):
    if row is None:
        raise RuntimeError('Run explicit init before scheduling Phase 5b')
    cfg = json.loads(row['config_json'])
    if digest(row['config_json']) != row['config_hash'] or cfg != specification():
        raise RuntimeError('Frozen Phase 5b source/config differs; restore preregistered code')
    if (row['version'] != VERSION or row['start_target'] % 3600 or
            row['end_target'] != row['start_target']+(LIMIT-1)*3600):
        raise RuntimeError('Invalid fixed Phase 5b target window')
    return cfg


def run(path, clock_fn=time.time, builder=None, call=None):
    from phase4_runner import build_snapshot
    from next_stages import regression_call, validate_rv
    builder = builder or build_snapshot
    call = call or regression_call
    clock = clock_fn()
    target = int(clock)//3600*3600
    # Reserve before any fetching/API. A crash leaves an auditable uncertain row.
    with database(path) as c:
        row = c.execute('SELECT * FROM phase5b_config').fetchone()
        checked_config(row)
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT * FROM phase5b_config').fetchone()
        if dict(current) != dict(row):
            raise RuntimeError('Phase 5b registration changed during verification')
        upper = min(target, row['end_target'])
        for slot in range(row['start_target'], upper+1, 3600):
            eligible = slot == target and clock-slot <= DEADLINE
            c.execute('''INSERT OR IGNORE INTO phase5b_predictions
                (target_candle_time,started_at,completed_at,status,version,error)
                VALUES(?,?,?,?,?,?)''',
                (slot, now(), None if eligible else now(),
                 'reserved' if eligible else 'missed', VERSION,
                 None if eligible else 'Scheduling window missed; no hindsight prediction'))
        if not row['start_target'] <= target <= row['end_target']:
            return
        claimed = c.execute("UPDATE phase5b_predictions SET status='started' WHERE target_candle_time=? AND status='reserved'", (target,)).rowcount
        if not claimed:
            return
    model = raw = None
    try:
        snapshot, previous = builder(target)
        previous = validate_rv(previous)
        with database(path) as c:
            c.execute('''UPDATE phase5b_predictions SET context=?,snapshot_hash=?,previous_rv=?
                WHERE target_candle_time=?''', (snapshot, digest(snapshot), previous, target))
        if not 0 <= clock_fn()-target <= DEADLINE:
            raise TimeoutError('Snapshot completed after prediction deadline; no API call')
        rv, model, raw = call(snapshot)
        rv = validate_rv(rv)
        late = not 0 <= clock_fn()-target <= DEADLINE
        with database(path) as c:
            c.execute('''UPDATE phase5b_predictions SET status=?,completed_at=?,predicted_rv=?,
                model_version=?,raw_json=?,error=? WHERE target_candle_time=?''',
                ('late' if late else 'complete', now(), rv, model, raw,
                 'Response after deadline; excluded from analysis' if late else None, target))
    except Exception as exc:
        with database(path) as c:
            c.execute('''UPDATE phase5b_predictions SET status='failed',completed_at=?,error=?,
                model_version=?,raw_json=? WHERE target_candle_time=?''',
                (now(), str(exc), getattr(exc, 'model_version', model),
                 getattr(exc, 'raw_json', raw), target))
        # Failed slots do not stop following hours or Phase 6.
        print('Phase 5b failed:', target, str(exc), flush=True)


def evaluate(path, candles_path, clock=None):
    from candle_store import read_window
    from volatility import realized_volatility
    clock = time.time() if clock is None else clock
    with database(path) as c:
        rows = c.execute('''SELECT target_candle_time FROM phase5b_predictions
            WHERE evaluated_at IS NULL AND target_candle_time+3660<=?''', (clock,)).fetchall()
    for row in rows:
        target = row[0]
        candles = read_window(candles_path, target)
        try:
            rv = realized_volatility(list(candles.values()), target)
        except ValueError:
            continue
        with database(path) as c:
            c.execute('''UPDATE phase5b_predictions SET actual_rv=?,evaluated_at=?
                WHERE target_candle_time=? AND evaluated_at IS NULL''', (rv, now(), target))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('init', 'cycle', 'status'))
    p.add_argument('--db', type=Path, default=ROOT/'btc.db')
    p.add_argument('--candles-db', type=Path, default=ROOT/'candles.db')
    args = p.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    if args.action == 'init':
        print(json.dumps(initialize(args.db), indent=2))
    elif args.action == 'cycle':
        # Prediction has priority; actuals remain recoverable on later cycles.
        run(args.db)
        evaluate(args.db, args.candles_db)
    else:
        with sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro', uri=True) as c:
            print('Window:', c.execute('SELECT start_target,end_target,version FROM phase5b_config').fetchall())
            print('Slots:', c.execute('SELECT status,count(*) FROM phase5b_predictions GROUP BY status').fetchall())
            print('Actuals pending:', c.execute('SELECT count(*) FROM phase5b_predictions WHERE evaluated_at IS NULL').fetchone()[0])


if __name__ == '__main__':
    main()
