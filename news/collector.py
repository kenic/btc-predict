"""Bounded public HTTP collection; no experiment imports, article requests or AI calls."""
import email.utils
import fcntl
import json
import logging
import random
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import store
from .feeds import MAX_BYTES, digest, parse_rss

LOG = logging.getLogger('btc.news')
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.scheme != 'https' or new.hostname != old.hostname or new.username or new.password or new.query:
            raise ValueError('unsafe_redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, validators):
    headers = {'User-Agent': 'btc-predict-news/1.0 (public RSS research)',
               'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml',
               'Accept-Encoding': 'identity'}
    if validators.get('etag'):
        headers['If-None-Match'] = validators['etag']
    if validators.get('last_modified'):
        headers['If-Modified-Since'] = validators['last_modified']
    try:
        response = build_opener(SafeRedirect()).open(Request(url, headers=headers), timeout=15)
    except HTTPError as e:
        with e:
            # Error pages may contain echoed credentials; never read or persist them.
            return e.code, {k.lower(): v for k, v in e.headers.items()}, b''
    with response:
        body = response.read(MAX_BYTES+1)
        if len(body) > MAX_BYTES:
            raise ValueError('oversize_response')
        return response.status, {k.lower(): v for k, v in response.headers.items()}, body


def retry_delay(value, now):
    try:
        return max(0, float(int(value)))
    except (ValueError, TypeError):
        try:
            d = email.utils.parsedate_to_datetime(value)
            return max(0, d.timestamp()-now) if d.tzinfo else 0
        except (ValueError, TypeError, OverflowError):
            return 0


@contextmanager
def collection_lock(path):
    # Also protects against manual invocations overlapping the systemd oneshot.
    with open(str(path)+'.collector.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('collector_already_running') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def collect_feed(path, source, interval, fetcher=fetch, clock=time.time, sleep=time.sleep):
    run_id = store.start_run(path, source, interval, clock)
    finish = lambda status, **kw: store.finish_run(path, run_id, status, clock=clock, **kw)
    if not source['enabled'] or source['policy']['mode'] == 'blocked':
        return finish('blocked', error='policy_or_config_disabled')
    with store.connect(path, True) as c:
        defer = c.execute('''SELECT max(a.retry_after) FROM attempts a JOIN runs r ON r.id=a.run_id
            WHERE r.feed_id=?''', (source['id'],)).fetchone()[0]
    if defer and defer > clock():
        return finish('deferred', error='retry_after_active')
    validators = store.validators(path, source['id'], source)
    for attempt in range(3):
        at, code, headers, body = clock(), None, {}, b''
        try:
            code, headers, body = fetcher(source['url'], validators)
            error = None if code in (200, 304) else 'http_error'
        except (URLError, TimeoutError, OSError):
            error = 'network_error'
        except ValueError:
            error = 'unsafe_response'
        retry = error == 'network_error' or code in RETRYABLE
        delay = max(retry_delay(headers.get('retry-after'), clock()), (2**attempt)+random.uniform(0, .5)) if retry else 0
        store.record_attempt(path, run_id, at, code, error, clock()+delay if retry else None)
        if error:
            if retry and attempt < 2 and delay <= 10:
                sleep(delay)
                continue
            return finish('failed', http_status=code, error=error)
        if code == 304:
            if not validators:
                return finish('failed', http_status=304, error='unsolicited_304')
            # Re-parse the confirmed bytes with the current adapter: a parser fix
            # becomes newly known NOW and never rewrites past normalized versions.
            with store.connect(path, True) as c:
                prior = c.execute("SELECT raw_body FROM runs WHERE feed_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
                                  (source['id'],)).fetchone()
            try:
                entries, rejected = parse_rss(prior['raw_body'], source)
            except (ValueError, SyntaxError, TypeError):
                return finish('failed', http_status=304, error='invalid_cached_feed')
            return finish('not_modified', entries=entries, rejected=rejected, http_status=304)
        try:
            entries, rejected = parse_rss(body, source)
        except (ValueError, SyntaxError):
            return finish('failed', http_status=code, error='invalid_feed')
        # Failed/HTML responses are never retained, even for a licensed feed.
        return finish('ok', entries=entries, rejected=rejected, body=body,
                      body_hash=digest(body), headers=headers, http_status=code)


def collect(path, config, fetcher=fetch, clock=time.time, sleep=time.sleep):
    results = []
    with collection_lock(path):
        store.initialize(path)
        for source in config['feeds']:
            result = collect_feed(path, source, config['interval_seconds'], fetcher, clock, sleep)
            # Structured log contains only stable ids, counts and error categories.
            LOG.info(json.dumps(dict(feed_id=source['id'], **result), sort_keys=True))
            results.append(result)
    return results
