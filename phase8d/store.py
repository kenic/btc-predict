"""Append-only event inputs, durable dispatch reservations and read-only reporting."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .core import canonical,digest,source_manifest

@contextmanager
def connect(path, readonly=False):
    c=sqlite3.connect(Path(path).resolve().as_uri()+('?mode=ro' if readonly else '?mode=rwc'),uri=True,timeout=20)
    c.row_factory=sqlite3.Row
    try:
        if readonly:c.execute('PRAGMA query_only=ON')
        yield c
    finally:c.close()

def initialize(path,config,now):
    with connect(path) as c,c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS seen(symbol TEXT,key TEXT,PRIMARY KEY(symbol,key));
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,symbol TEXT,cutoff REAL,target REAL,kind TEXT,
          status TEXT,error TEXT,trigger_json TEXT,input_json TEXT,created REAL,reference_quote INTEGER);
        CREATE TABLE IF NOT EXISTS calls(event TEXT,engine TEXT,status TEXT,reserved_at REAL,reserved_usd REAL,
          result_json TEXT,PRIMARY KEY(event,engine));
        CREATE TABLE IF NOT EXISTS evaluations(event TEXT PRIMARY KEY,result_json TEXT);
        CREATE TABLE IF NOT EXISTS health(worker TEXT PRIMARY KEY,observed REAL,status TEXT);
        ''')
        contract=digest({'config':config,'sources':source_manifest()})
        old=c.execute("SELECT value FROM settings WHERE key='contract'").fetchone()
        if old and old[0]!=contract:raise ValueError('immutable_contract_changed_use_new_archive')
        c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('contract',contract))
        c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('config',canonical(config)))
        c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('started',str(now)))

def heartbeat(path,worker,status,now):
    with connect(path) as c,c:c.execute('INSERT OR REPLACE INTO health VALUES(?,?,?)',(worker,now,status))
