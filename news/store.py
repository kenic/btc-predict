"""SQLite provenance, append-only version transitions and point-in-time reads."""
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

APP_ID = 0x4E455753
SCHEMA = '''
CREATE TABLE IF NOT EXISTS feeds (
 id TEXT PRIMARY KEY, first_configured_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, feed_id TEXT NOT NULL REFERENCES feeds(id),
 started_at REAL NOT NULL, finished_at REAL, available_at REAL,
 status TEXT NOT NULL, source_json TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
 http_status INTEGER, error TEXT, raw_body BLOB, body_hash TEXT,
 etag TEXT, last_modified TEXT,
 entries INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0,
 duplicates INTEGER NOT NULL DEFAULT 0, revisions INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS runs_feed_time ON runs(feed_id, available_at);
CREATE TABLE IF NOT EXISTS attempts (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
 attempted_at REAL NOT NULL, status INTEGER, error TEXT, retry_after REAL);
CREATE TABLE IF NOT EXISTS articles (
 id INTEGER PRIMARY KEY, feed_id TEXT NOT NULL REFERENCES feeds(id),
 first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS aliases (
 feed_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
 article_id INTEGER NOT NULL REFERENCES articles(id), PRIMARY KEY(feed_id,kind,value));
CREATE TABLE IF NOT EXISTS versions (
 id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL REFERENCES articles(id),
 run_id INTEGER NOT NULL REFERENCES runs(id), available_at REAL NOT NULL,
 content_hash TEXT NOT NULL, raw_hash TEXT NOT NULL, item_json TEXT NOT NULL, raw_item TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS versions_asof ON versions(article_id,available_at,id);
CREATE TABLE IF NOT EXISTS observations (
 run_id INTEGER NOT NULL REFERENCES runs(id), article_id INTEGER NOT NULL REFERENCES articles(id),
 version_id INTEGER NOT NULL REFERENCES versions(id), observed_at REAL NOT NULL,
 PRIMARY KEY(run_id,article_id));
'''


@contextmanager
def connect(path, readonly=False):
    if readonly:
        c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    else:
        c = sqlite3.connect(path, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        if readonly:
            c.execute('PRAGMA query_only=ON')
        yield c
    finally:
        c.close()


def initialize(path):
    if Path(path).name in ('btc.db', 'market.db', 'candles.db'):
        raise ValueError('dedicated_news_db_required')
    with connect(path) as c:
        app_id = c.execute('PRAGMA application_id').fetchone()[0]
        tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if app_id not in (0, APP_ID) or (app_id == 0 and tables):
            raise ValueError('not_a_news_database')
        if c.execute('PRAGMA user_version').fetchone()[0] > 1:
            raise ValueError('unsupported_news_schema')
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript(SCHEMA)
        c.execute('PRAGMA application_id=%d' % APP_ID)
        c.execute('PRAGMA user_version=1')


def start_run(path, source, interval, clock=time.time):
    now = clock()
    with connect(path) as c, c:
        c.execute('INSERT OR IGNORE INTO feeds VALUES(?,?)', (source['id'], now))
        # A killed process leaves a visible running record; never infer success.
        return c.execute('''INSERT INTO runs(feed_id,started_at,status,source_json,interval_seconds)
            VALUES(?,?,?,?,?)''', (source['id'], now, 'running', json.dumps(source, sort_keys=True), interval)).lastrowid


def record_attempt(path, run_id, at, status=None, error=None, retry_after=None):
    with connect(path) as c, c:
        c.execute('INSERT INTO attempts(run_id,attempted_at,status,error,retry_after) VALUES(?,?,?,?,?)',
                  (run_id, at, status, error, retry_after))


def validators(path, feed_id, source):
    with connect(path, True) as c:
        row = c.execute('''SELECT etag,last_modified,source_json FROM runs WHERE feed_id=?
            AND status='ok' ORDER BY id DESC LIMIT 1''', (feed_id,)).fetchone()
        # Changed endpoint or policy must be fetched again, not validated against an old body.
        if row and row['source_json'] == json.dumps(source, sort_keys=True):
            return {k: row[k] for k in ('etag', 'last_modified') if row[k]}
        return {}


def finish_run(path, run_id, status, *, entries=(), body=None, body_hash=None,
               rejected=0, headers=None, http_status=None, error=None, clock=time.time):
    """Network and parsing have finished before this transaction starts.

    available_at is local ingestion time, never a publisher/header timestamp.
    Reversion A->B->A is a new transition, rather than reusing A's first timestamp.
    """
    headers = headers or {}
    with connect(path) as c, c:
        c.execute('BEGIN IMMEDIATE')
        run = c.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if run['status'] != 'running':
            raise ValueError('run_already_finished')
        now = clock()
        latest = c.execute('SELECT max(available_at) FROM runs').fetchone()[0]
        if now < run['started_at'] or (latest is not None and now < latest):
            raise ValueError('clock_moved_backwards')
        revisions = duplicates = 0
        seen = set()
        # Reject ambiguous duplicate identities within one response, never let XML order decide.
        groups = {}
        for entry in entries:
            groups.setdefault(entry['item']['url'], []).append(entry)
        clean = []
        for group in groups.values():
            if len({e['raw_hash'] for e in group}) > 1:
                rejected += len(group)
            else:
                clean.append(group[0])
                duplicates += len(group) - 1
        guid_hashes = {}
        for entry in clean:
            if entry['guid']:
                guid_hashes.setdefault(entry['guid'], set()).add(entry['raw_hash'])
        for entry in clean:
            if entry['guid'] and len(guid_hashes[entry['guid']]) > 1:
                rejected += 1
                continue
            aliases = [('url', entry['item']['url'])]
            if entry['guid']:
                aliases.append(('guid', entry['guid']))
            ids = {r[0] for kind, value in aliases for r in c.execute(
                'SELECT article_id FROM aliases WHERE feed_id=? AND kind=? AND value=?',
                (run['feed_id'], kind, value))}
            if len(ids) > 1:
                rejected += 1
                continue
            article_id = next(iter(ids)) if ids else None
            if article_id in seen:
                rejected += 1
                continue
            if article_id is None:
                article_id = c.execute('INSERT INTO articles(feed_id,first_seen_at,last_seen_at) VALUES(?,?,?)',
                                       (run['feed_id'], now, now)).lastrowid
            seen.add(article_id)
            for kind, value in aliases:
                c.execute('INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)', (run['feed_id'], kind, value, article_id))
            prior = c.execute('SELECT * FROM versions WHERE article_id=? ORDER BY id DESC LIMIT 1',
                              (article_id,)).fetchone()
            prior_source = c.execute('SELECT source_json FROM runs WHERE id=?', (prior['run_id'],)).fetchone()[0] if prior else None
            if (prior and prior['raw_hash'] == entry['raw_hash']
                    and prior['content_hash'] == entry['content_hash'] and prior_source == run['source_json']):
                version_id = prior['id']
                duplicates += 1
            else:
                version_id = c.execute('''INSERT INTO versions(article_id,run_id,available_at,content_hash,
                    raw_hash,item_json,raw_item) VALUES(?,?,?,?,?,?,?)''',
                    (article_id, run_id, now, entry['content_hash'], entry['raw_hash'],
                     json.dumps(entry['item'], sort_keys=True), entry['raw'])).lastrowid
                revisions += int(prior is not None)
            c.execute('UPDATE articles SET last_seen_at=? WHERE id=?', (now, article_id))
            c.execute('INSERT INTO observations VALUES(?,?,?,?)', (run_id, article_id, version_id, now))
        c.execute('''UPDATE runs SET finished_at=?,available_at=?,status=?,http_status=?,error=?,
            raw_body=?,body_hash=?,etag=?,last_modified=?,entries=?,rejected=?,duplicates=?,revisions=? WHERE id=?''',
            (now, now, status, http_status, error, body, body_hash, headers.get('etag'),
             headers.get('last-modified'), len(entries), rejected, duplicates, revisions, run_id))
    return dict(run_id=run_id, status=status, entries=len(entries), rejected=rejected,
                duplicates=duplicates, revisions=revisions)


def health(path, as_of=None):
    now = time.time() if as_of is None else as_of
    result = []
    with connect(path, True) as c:
        for feed in c.execute('SELECT * FROM feeds WHERE first_configured_at<=? ORDER BY id', (now,)):
            runs = c.execute('SELECT * FROM runs WHERE feed_id=? AND started_at<=? ORDER BY started_at,id',
                             (feed['id'], now)).fetchall()
            completed = [r for r in runs if r['available_at'] is not None and r['available_at'] <= now]
            successful = [r for r in completed if r['status'] in ('ok', 'not_modified')]
            last = completed[-1] if completed else None
            source = json.loads((last or runs[0])['source_json'])
            interval = (last or runs[0])['interval_seconds']
            # Gaps describe unobserved polling time, not an unknowable number of lost articles.
            points = [feed['first_configured_at']] + [r['available_at'] for r in successful] + [now]
            gaps = [dict(start=a, end=b, seconds=b-a) for a, b in zip(points, points[1:]) if b-a > interval*1.5]
            total = sum(r['entries'] for r in completed)
            duplicates = sum(r['duplicates'] for r in completed)
            deferred = c.execute('''SELECT max(a.retry_after) FROM attempts a JOIN runs r ON r.id=a.run_id
                WHERE r.feed_id=? AND a.attempted_at<=? AND r.available_at<=?''',
                (feed['id'], now, now)).fetchone()[0]
            result.append(dict(feed_id=feed['id'], configured_at=feed['first_configured_at'],
                enabled=source['enabled'], latest_status=last['status'] if last else 'running',
                last_success_at=successful[-1]['available_at'] if successful else None,
                stale=not successful or now-successful[-1]['available_at'] > interval*1.5,
                latest_entries=last['entries'] if last else 0,
                latest_rejected=last['rejected'] if last else 0,
                last_http_status=last['http_status'] if last else None,
                last_error=last['error'] if last else None,
                next_retry_at=deferred if deferred and deferred>now else None,
                gaps=gaps, failed_runs=sum(r['status']=='failed' for r in completed),
                interrupted_or_running=sum(r['available_at'] is None or r['available_at'] > now for r in runs),
                rejected=sum(r['rejected'] for r in completed), entries=total,
                duplicate_rate=duplicates/total if total else 0,
                coverage='polling_only; feed truncation and between-poll revisions may be unrecoverable'))
    return result


def snapshot(path, as_of, hours=24, deduplicate=True):
    """Latest known version FIRST, publication-window filter SECOND (no fallback leak)."""
    if not math.isfinite(as_of) or hours <= 0 or hours > 24*366 or as_of < 0:
        raise ValueError('invalid_window')
    with connect(path, True) as c:
        # One SQLite read snapshot keeps article/provenance joins mutually consistent.
        c.execute('BEGIN')
        rows = c.execute('''SELECT v.*,a.feed_id,a.first_seen_at,r.source_json FROM versions v
            JOIN articles a ON a.id=v.article_id JOIN runs r ON r.id=v.run_id
            WHERE v.available_at<=? AND NOT EXISTS (
                SELECT 1 FROM versions newer WHERE newer.article_id=v.article_id AND newer.available_at<=?
                AND (newer.available_at>v.available_at OR
                     (newer.available_at=v.available_at AND newer.id>v.id)))
            ORDER BY v.article_id''', (as_of, as_of)).fetchall()
        news, excluded = [], dict(missing_publication=0, outside_window=0, policy=0)
        for row in rows:
            item = json.loads(row['item_json'])
            source = json.loads(row['source_json'])
            policy = source['policy']
            if not policy.get('model_input', False):
                excluded['policy'] += 1
                continue
            p = item['published_at']
            if p is None:
                excluded['missing_publication'] += 1
                continue
            if not as_of-hours*3600 < p <= as_of:
                excluded['outside_window'] += 1
                continue
            last_seen = c.execute('SELECT max(observed_at) FROM observations WHERE article_id=? AND observed_at<=?',
                                  (row['article_id'], as_of)).fetchone()[0]
            news.append(dict(item, article_id=row['article_id'], version_id=row['id'], source_id=row['feed_id'],
                available_at=row['available_at'], first_fetched_at=row['first_seen_at'], last_fetched_at=last_seen,
                publisher=source['publisher'], category=source.get('category'),
                content_sha256=row['content_hash'], raw_sha256=row['raw_hash'],
                ingestion_lag_seconds=row['available_at']-p, duplicate_of=None))
    news.sort(key=lambda x: (-x['published_at'], x['source_id'], x['article_id']))
    seen, output = {}, []
    for item in news:
        # Exact canonical URLs only; similar headlines are not sufficient evidence.
        if item['url'] in seen:
            item['duplicate_of'] = seen[item['url']]
            if deduplicate:
                continue
        else:
            seen[item['url']] = item['article_id']
        output.append(item)
    return dict(schema_version='phase8pre.news.v1', as_of=as_of, window_start_exclusive=as_of-hours*3600,
                window_end_inclusive=as_of, selection='all_eligible; canonical_url_dedup='+str(deduplicate).lower(),
                news=output, excluded=excluded,
                coverage=health(path, as_of), historical_backfill=False)
