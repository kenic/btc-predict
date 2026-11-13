"""Receipt-timed public quotes. No historical quote substitution or exchange orders."""
import json,time,urllib.request,math
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from .core import SYMBOLS,canonical

def collect(path,symbol,clock=time.time):
    if symbol not in SYMBOLS:raise ValueError('unsupported_symbol')
    started=clock()
    req=urllib.request.Request('https://api.exchange.coinbase.com/products/'+symbol+'/ticker',headers={'User-Agent':'btc-phase8d/1'})
    with urllib.request.urlopen(req,timeout=5) as r:
        body=r.read(65537)
    if len(body)>65536:raise ValueError('oversized_quote')
    raw=json.loads(body);observed=clock();bid=float(raw['bid']);ask=float(raw['ask'])
    if not all(math.isfinite(x) for x in (bid,ask)) or not 0<bid<=ask or not started<=observed<=started+10:
        raise ValueError('invalid_quote')
    with connect(path) as c,c:c.execute('INSERT INTO quotes(symbol,started,observed,bid,ask,raw_json) VALUES(?,?,?,?,?,?)',(symbol,started,observed,bid,ask,canonical(raw)))

def before(c,symbol,at):
    r=c.execute('SELECT * FROM quotes WHERE symbol=? AND observed<=? AND observed>=? ORDER BY observed DESC,id DESC LIMIT 1',(symbol,at,at-10)).fetchone()
    return dict(r) if r else None

def after(c,symbol,at):
    # Request itself begins after the decision: a response in flight cannot be an entry.
    r=c.execute('SELECT * FROM quotes WHERE symbol=? AND started>=? AND observed<=? ORDER BY observed,id LIMIT 1',(symbol,at,at+15)).fetchone()
    return dict(r) if r else None

def midpoint(q):return (q['bid']+q['ask'])/2

@contextmanager
def connect(path,readonly=False):
    c=sqlite3.connect(Path(path).resolve().as_uri()+('?mode=ro' if readonly else '?mode=rwc'),uri=True,timeout=20)
    c.row_factory=sqlite3.Row
    try:
        if readonly:c.execute('PRAGMA query_only=ON')
        yield c
    finally:c.close()

def initialize(path):
    with connect(path) as c,c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY,symbol TEXT,started REAL,observed REAL,bid REAL,ask REAL,raw_json TEXT); CREATE INDEX IF NOT EXISTS quote_time ON quotes(symbol,observed); CREATE INDEX IF NOT EXISTS quote_start ON quotes(symbol,started); CREATE TABLE IF NOT EXISTS health(worker TEXT PRIMARY KEY,observed REAL,status TEXT);')

def heartbeat(path,worker,status,now):
    with connect(path) as c,c:c.execute('INSERT OR REPLACE INTO health VALUES(?,?,?)',(worker,now,status))
