"""Generic, append-only BTC/USD 5m archive. No experiment dependencies or fetching."""
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / 'candles.db'
SECONDS = 300
SETTLE_SECONDS = 60
SOURCE = 'coinbase-exchange:BTC-USD:300'


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Expected finite number')
    return value


def validate(candle, start, end, clock):
    """Exact Coinbase array, requested half-open range, completed with safety delay."""
    if not isinstance(candle, (list, tuple)) or len(candle) not in (5, 6):
        raise ValueError('Malformed candle')
    t = numeric(candle[0])
    if t != int(t) or t % SECONDS or not start <= t < end:
        raise ValueError('Invalid/out-of-range start timestamp')
    if t + SECONDS + SETTLE_SECONDS > clock:
        raise ValueError('Candle incomplete or still settling')
    low, high, opening, close = (numeric(v) for v in candle[1:5])
    if min(low, high, opening, close) <= 0 or high < max(opening, close, low) or low > min(opening, close, high):
        raise ValueError('Invalid OHLC')
    volume = candle[5] if len(candle) == 6 else None
    if volume is not None and numeric(volume) < 0:
        raise ValueError('Invalid volume')
    return [int(t), low, high, opening, close, volume]


def initialize(path, clock):
    """Persist future-only floor on first invocation, even if its fetch later fails."""
    numeric(clock)
    if clock < 0:
        raise ValueError('Invalid clock')
    with closing(sqlite3.connect(path, timeout=5)) as c:
        c.execute('PRAGMA journal_mode=WAL')
        with c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('''CREATE TABLE IF NOT EXISTS collector_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                started_at INTEGER NOT NULL,
                first_start_ts INTEGER NOT NULL CHECK(first_start_ts % 300=0),
                source TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS candles_5m (
                start_ts INTEGER PRIMARY KEY CHECK(start_ts % 300=0),
                open REAL NOT NULL CHECK(open > 0), high REAL NOT NULL,
                low REAL NOT NULL CHECK(low > 0), close REAL NOT NULL CHECK(close > 0),
                volume REAL CHECK(volume IS NULL OR volume >= 0),
                source TEXT NOT NULL, fetched_at INTEGER NOT NULL, raw_json TEXT,
                CHECK(high >= max(open,close,low)),
                CHECK(low <= min(open,close,high)),
                CHECK(fetched_at >= start_ts+360))''')
            c.execute('INSERT OR IGNORE INTO collector_state VALUES(1,?,?,?)',
                      (int(clock), math.ceil(clock / SECONDS) * SECONDS, SOURCE))
            state = c.execute('SELECT first_start_ts,source FROM collector_state WHERE singleton=1').fetchone()
            if state[1] != SOURCE:
                raise ValueError('Archive source mismatch')
            return state[0]


def save(path, rows, fetched_at):
    """First valid observation wins; provider revisions never overwrite research data."""
    inserted = duplicates = conflicts = 0
    with closing(sqlite3.connect(path, timeout=5)) as c, c:
        floor, source = c.execute('SELECT first_start_ts,source FROM collector_state WHERE singleton=1').fetchone()
        if source != SOURCE:
            raise ValueError('Archive source mismatch')
        for raw in rows:
            candle = validate(raw, floor, int(fetched_at), fetched_at)
            t, low, high, opening, close, volume = candle
            values = (opening, high, low, close, volume)
            previous = c.execute('SELECT open,high,low,close,volume FROM candles_5m WHERE start_ts=?', (t,)).fetchone()
            if previous is not None:
                if previous == values:
                    duplicates += 1
                else:
                    conflicts += 1
                continue
            c.execute('INSERT INTO candles_5m VALUES(?,?,?,?,?,?,?,?,?)',
                      (t, *values, SOURCE, int(fetched_at), json.dumps(raw, separators=(',', ':'), allow_nan=False)))
            inserted += 1
    return dict(inserted=inserted, duplicates=duplicates, conflicts=conflicts)


def read_window(path, target):
    """Read-only exact 13-close window; absent/busy/broken stores are unavailable.

    Opening a missing path in read-only mode cannot create a DB or its schema.
    No network, writes, interpolation, or candle synthesis occurs here.
    """
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)) as c:
            c.execute('PRAGMA query_only=ON')
            rows = c.execute('''SELECT start_ts,low,high,open,close,volume,fetched_at,source
                FROM candles_5m WHERE start_ts>=? AND start_ts<? ORDER BY start_ts''',
                             (target-SECONDS, target+3600)).fetchall()
        if [r[0] for r in rows] != list(range(target-SECONDS, target+3600, SECONDS)):
            return {}
        candles = {}
        for row in rows:
            if row[7] != SOURCE:
                return {}
            candle = validate(row[:6], target-SECONDS, target+3600, row[6])
            candles[candle[0]] = candle
        return candles
    except (sqlite3.Error, OSError, ValueError, TypeError, IndexError, OverflowError):
        return {}
