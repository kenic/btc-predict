"""Dedicated SQLite store, immutable inputs and conservative durable call reservations."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .core import canonical, digest, source_manifest, ENGINES

APP_ID = 0x50384231
SCHEMA = '''
CREATE TABLE experiment(id INTEGER PRIMARY KEY CHECK(id=1), config_json TEXT NOT NULL, config_hash TEXT NOT NULL,
 source_json TEXT NOT NULL);
CREATE TABLE cohorts(cutoff INTEGER PRIMARY KEY, mode TEXT NOT NULL, created_at REAL NOT NULL,
 input_id TEXT NOT NULL, input_json TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE calls(cutoff INTEGER NOT NULL REFERENCES cohorts(cutoff), engine TEXT NOT NULL,
 status TEXT NOT NULL, reserved_usd REAL NOT NULL DEFAULT 0, reserved_at REAL,
 result_json TEXT, PRIMARY KEY(cutoff,engine));
CREATE TABLE outcomes(cutoff INTEGER PRIMARY KEY REFERENCES cohorts(cutoff), evaluated_at REAL NOT NULL,
 outcome_json TEXT NOT NULL);
'''


@contextmanager
def connect(path, readonly=False):
    uri = Path(path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw')
    c = sqlite3.connect(uri, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.execute('PRAGMA foreign_keys=ON')
        if readonly:
            c.execute('PRAGMA query_only=ON')
        if c.execute('PRAGMA application_id').fetchone()[0] != APP_ID:
            raise ValueError('not_phase8b_database')
        yield c
    finally:
        c.close()


def initialize(path, config):
    p = Path(path)
    if p.exists():
        with connect(p, True) as c:
            if c.execute('SELECT config_hash FROM experiment').fetchone()[0] != digest(config):
                raise ValueError('experiment_config_is_frozen_use_new_database')
            if c.execute('SELECT source_json FROM experiment').fetchone()[0] != canonical(source_manifest()):
                raise ValueError('experiment_code_changed_use_new_database')
        return
    if p.name in ('btc.db', 'candles.db', 'market.db', 'news.db'):
        raise ValueError('dedicated_phase8b_database_required')
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    c = sqlite3.connect(p)
    try:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript(SCHEMA)
        c.execute('PRAGMA application_id=%d' % APP_ID)
        c.execute('PRAGMA user_version=1')
        with c:
            c.execute('INSERT INTO experiment VALUES(1,?,?,?)',
                      (canonical(config), digest(config), canonical(source_manifest())))
    finally:
        c.close()


def add_cohort(path, cutoff, now, frozen, mode):
    with connect(path) as c, c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM cohorts WHERE cutoff=?', (cutoff,)).fetchone():
            return False
        c.execute('INSERT INTO cohorts VALUES(?,?,?,?,?,?)',
                  (cutoff, mode, now, frozen['input_id'], canonical(frozen), 'frozen'))
        c.executemany('INSERT INTO calls(cutoff,engine,status) VALUES(?,?,?)',
                      [(cutoff, e, 'pending') for e in ENGINES])
        return True


def reserve_all(path, cutoff, now, config):
    """Never refund automatically, even on errors/crash. Reserving once prevents replay."""
    with connect(path) as c, c:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute('SELECT status FROM calls WHERE cutoff=?', (cutoff,)).fetchall()
        if len(rows) != 4 or any(r[0] != 'pending' for r in rows):
            raise ValueError('already_attempted')
        latest = c.execute('SELECT max(reserved_at) FROM calls').fetchone()[0]
        if latest is not None and now < latest:
            raise ValueError('clock_moved_backwards')
        total, count = c.execute('SELECT coalesce(sum(reserved_usd),0),count(reserved_at) FROM calls').fetchone()
        daily = c.execute('SELECT coalesce(sum(reserved_usd),0) FROM calls WHERE reserved_at>=?',
                          (int(now)//86400*86400,)).fetchone()[0]
        amount = config['reserve_per_call_usd'] * 4
        if (total+amount > config['total_budget_usd']+1e-9 or daily+amount > config['daily_budget_usd']+1e-9
                or count+4 > config['max_calls']):
            raise ValueError('budget_or_call_limit')
        c.execute("UPDATE calls SET status='reserved',reserved_usd=?,reserved_at=? WHERE cutoff=?",
                  (config['reserve_per_call_usd'], now, cutoff))


def finish(path, cutoff, engine, result):
    with connect(path) as c, c:
        c.execute('UPDATE calls SET status=?,result_json=? WHERE cutoff=? AND engine=? AND result_json IS NULL',
                  (result['status'], canonical(result), cutoff, engine))


def finish_cohort(path, cutoff, status):
    with connect(path) as c, c:
        c.execute('UPDATE cohorts SET status=? WHERE cutoff=?', (status, cutoff))
