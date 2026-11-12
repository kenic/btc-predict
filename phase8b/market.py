"""Explicit read-only Coinbase price retrieval, isolated from the BTC candle service."""
import json
import sqlite3
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from candle_store import validate
from .core import SYMBOLS, canonical

APP_ID = 0x50384D31


def source(symbol):
    if symbol not in SYMBOLS:
        raise ValueError('unsupported_symbol')
    return 'coinbase-exchange:' + symbol + ':300'


def connect(path):
    p = Path(path)
    existed = p.exists()
    c = sqlite3.connect(p)
    if existed and c.execute('PRAGMA application_id').fetchone()[0] != APP_ID:
        c.close()
        raise ValueError('not_multiasset_price_database')
    if not existed:
        c.execute('PRAGMA application_id=%d' % APP_ID)
        c.execute('''CREATE TABLE prices(symbol TEXT, start_ts INTEGER, raw_json TEXT,
                     fetched_at REAL, source TEXT, PRIMARY KEY(symbol,start_ts))''')
        c.commit()
    c.row_factory = sqlite3.Row
    return c


def fetch(path, symbol, cutoff, horizon, clock=time.time):
    """One unauthenticated GET per exact completed candle. Never retries or trades."""
    src = source(symbol)
    now = clock()
    saved = 0
    with closing(connect(path)) as c:
        for ts in (cutoff-300, cutoff+300, cutoff+horizon-300):
            if now < ts+360 or c.execute('SELECT 1 FROM prices WHERE symbol=? AND start_ts=?', (symbol, ts)).fetchone():
                continue
            iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
            query = urlencode(dict(start=iso(ts), end=iso(ts+300), granularity=300))
            req = urllib.request.Request('https://api.exchange.coinbase.com/products/'+symbol+'/candles?'+query,
                                         headers={'User-Agent': 'phase8b-price-evaluation/1'})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read(65537)
            if len(body) > 65536:
                raise ValueError('price_response_too_large')
            rows = json.loads(body)
            matches = [row for row in rows if row[0] == ts]
            if len(matches) != 1:
                continue
            observed = clock()
            row = validate(matches[0], ts, ts+300, observed)
            with c:
                saved += c.execute('INSERT OR IGNORE INTO prices VALUES(?,?,?,?,?)',
                                   (symbol, ts, canonical(row), observed, src)).rowcount
    return {'symbol': symbol, 'saved': saved}


def prices(path, symbol, cutoff, horizon, now):
    expected = source(symbol)
    if now < cutoff+horizon+60 or not Path(path).exists():
        return None
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as c:
        c.row_factory = sqlite3.Row
        if c.execute('PRAGMA application_id').fetchone()[0] != APP_ID:
            raise ValueError('not_multiasset_price_database')
        c.execute('BEGIN')
        rows = [c.execute('SELECT * FROM prices WHERE symbol=? AND start_ts=?', (symbol, t)).fetchone()
                for t in (cutoff-300, cutoff+300, cutoff+horizon-300)]
    if any(r is None or r['source'] != expected or r['fetched_at'] > now for r in rows):
        return None
    candles = [validate(json.loads(r['raw_json']), r['start_ts'], r['start_ts']+300, r['fetched_at']) for r in rows]
    ref, entry, end = candles
    return {'symbol': symbol, 'reference_price': ref[4], 'end_price': end[4],
            'price_up': int(end[4] > ref[4]), 'flat': end[4] == ref[4],
            'forecast_return': end[4]/ref[4]-1, 'paper_entry_at': cutoff+300,
            'paper_exit_at': cutoff+horizon, 'paper_entry_price': entry[3],
            'paper_return': end[4]/entry[3]-1, 'price_source': expected,
            'candles': [dict(r) for r in rows]}
