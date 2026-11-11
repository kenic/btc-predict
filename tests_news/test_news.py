import copy
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from news import store
from news.cli import main
from news.collector import collect, collect_feed, collection_lock, retry_delay
from news.feeds import canonical_url, load_config, parse_rss, timestamp, MAX_BYTES

ROOT = Path(__file__).resolve().parent.parent
PUB = 1700000000


def rss(title='Original', published='2023-11-14T22:13:20Z', guid='guid-1', url='https://example.org/news/a', extra=''):
    return ('<rss version="2.0"><channel><title>Test feed</title><item><guid>'+guid+'</guid><title>'+title+
            '</title><link>'+url+'</link><pubDate>'+published+'</pubDate><description>RSS summary</description>'+extra+
            '</item></channel></rss>').encode()


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/'news.db'
        self.config = load_config(ROOT/'news.config.example.json')
        self.source = copy.deepcopy(self.config['feeds'][1])
        self.now = PUB+100
        self.clock = lambda: self.now
        store.initialize(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, body=None, code=200, headers=None):
        return collect_feed(self.db, self.source, 900,
            Mock(return_value=(code, headers or {}, body or rss())), self.clock, Mock())

    def query(self, t=None, **kw):
        return store.snapshot(self.db, self.now if t is None else t, **kw)

    def sql(self, sql, args=()):
        with store.connect(self.db, True) as c:
            return [tuple(r) for r in c.execute(sql, args)]

    def test_late_arrival_revision_reversion_and_asof_last_seen(self):
        self.ingest()
        first = self.query()['news'][0]
        self.assertEqual(self.query(PUB+99)['news'], [])
        self.now += 900
        self.ingest(rss('Revision'))
        self.now += 900
        self.ingest(rss('Original'))
        now = self.query()['news'][0]
        self.assertNotEqual(first['version_id'], now['version_id'])
        self.assertEqual(first['content_sha256'], now['content_sha256'])
        historical = self.query(PUB+100)['news'][0]
        self.assertEqual(historical, first)
        self.assertEqual(self.query(PUB+1000)['news'][0]['title'], 'Revision')
        self.assertEqual(self.sql('SELECT count(*) FROM versions'), [(3,)])

    def test_availability_after_fetch_not_request_start(self):
        def delayed(url, validators):
            self.now += 30
            return 200, {}, rss()
        collect_feed(self.db, self.source, 900, delayed, self.clock, Mock())
        self.assertEqual(self.query(PUB+120)['news'], [])
        self.assertEqual(len(self.query(PUB+130)['news']), 1)

    def test_latest_version_selected_before_publication_filter(self):
        self.ingest()
        self.now += 900
        self.ingest(rss('Revision', '2023-11-16T00:00:00Z'))
        self.assertEqual(self.query()['news'], [])  # no fallback to the old version
        self.assertEqual(len(self.query(PUB+100)['news']), 1)

    def test_window_boundaries_missing_dates_and_no_backfill(self):
        self.ingest()
        self.assertEqual(len(self.query(PUB)['news']), 0)  # not yet acquired
        self.assertEqual(len(self.query(PUB+86399)['news']), 1)
        self.assertEqual(len(self.query(PUB+86400)['news']), 0)  # lower bound exclusive
        self.now += 900
        self.ingest(rss('No date', 'ambiguous'))
        self.assertEqual(self.query()['excluded']['missing_publication'], 1)

    def test_idempotence_raw_retention_and_conditional_304(self):
        body = rss()
        self.ingest(body, headers={'etag':'"one"', 'last-modified':'Tue, 14 Nov 2023 22:13:20 GMT'})
        self.now += 900
        fetcher = Mock(return_value=(304, {}, b''))
        collect_feed(self.db, self.source, 900, fetcher, self.clock, Mock())
        self.assertEqual(fetcher.call_args[0][1]['etag'], '"one"')
        self.assertEqual(self.query()['news'][0]['last_fetched_at'], self.now)
        self.assertEqual(self.sql('SELECT count(*) FROM versions'), [(1,)])
        self.assertEqual(self.sql('SELECT raw_body FROM runs WHERE status="ok"'), [(body,)])
        self.assertEqual(self.sql('SELECT body_hash FROM runs WHERE status="ok"'), [(hashlib.sha256(body).hexdigest(),)])

    def test_guid_changes_url_tracking_and_url_changes(self):
        self.ingest()
        self.now += 900
        self.ingest(rss(guid='changed-guid', url='https://example.org/news/a?utm_source=mail'))
        self.now += 900
        self.ingest(rss(guid='changed-guid', url='https://example.org/news/new'))
        self.assertEqual(self.sql('SELECT count(*) FROM articles'), [(1,)])
        self.assertEqual(self.query()['news'][0]['url'], 'https://example.org/news/new')

    def test_cross_feed_url_duplicates_keep_provenance(self):
        self.ingest()
        self.source['id'] = 'second_feed'
        self.ingest()
        self.assertEqual(len(self.query()['news']), 1)
        rows = self.query(deduplicate=False)['news']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['duplicate_of'], rows[0]['article_id'])

    def test_same_headline_different_urls_not_merged(self):
        self.ingest()
        self.ingest(rss(guid='g2', url='https://example.org/news/b'))
        self.assertEqual(len(self.query()['news']), 2)

    def test_ambiguous_response_does_not_choose_first_url_version(self):
        other_item = b'<item>'+rss('Conflict').split(b'<item>')[1].split(b'</channel>')[0]
        body = rss().replace(b'</channel>', other_item+b'</channel>')
        result = self.ingest(body)
        self.assertEqual(result['rejected'], 2)
        self.assertEqual(self.query()['news'], [])

    def test_ambiguous_guid_with_different_urls_rejected(self):
        other_item = b'<item>'+rss(url='https://example.org/b').split(b'<item>')[1].split(b'</channel>')[0]
        result = self.ingest(rss().replace(b'</channel>',other_item+b'</channel>'))
        self.assertEqual(result['rejected'],2)
        self.assertEqual(self.query()['news'],[])

    def test_policy_change_with_identical_raw_creates_provenance_version(self):
        self.ingest()
        self.now += 900
        self.source['policy']['model_input'] = False
        self.ingest()
        self.assertEqual(self.query()['news'],[])
        self.assertEqual(self.sql('SELECT count(*) FROM versions'),[(2,)])
        self.assertEqual(len(self.query(PUB+100)['news']),1)

    def test_inflight_health_is_reproducible_after_finish(self):
        run_id = store.start_run(self.db,self.source,900,self.clock)
        initial = store.health(self.db,self.now)
        self.now += 100
        store.record_attempt(self.db,run_id,PUB+100,429,'http_error',self.now+3600)
        store.finish_run(self.db,run_id,'failed',clock=self.clock)
        self.assertEqual(store.health(self.db,PUB+100),initial)

    def test_unsolicited_304_not_a_success(self):
        self.assertEqual(self.ingest(code=304)['status'],'failed')

    def test_endpoint_change_drops_validators(self):
        self.ingest(headers={'etag':'"old"'})
        self.source['url'] = 'https://www.federalreserve.gov/feeds/press_monetary.xml'
        fetcher = Mock(return_value=(200,{},rss()))
        collect_feed(self.db,self.source,900,fetcher,self.clock,Mock())
        self.assertEqual(fetcher.call_args[0][1],{})

    def test_parser_correction_on_304_is_new_knowledge(self):
        parsed, rejected = parse_rss(rss(),self.source)
        wrong = copy.deepcopy(parsed)
        wrong[0]['item']['published_at'] = None
        wrong[0]['content_hash'] = 'earlier-parser-version'
        with patch('news.collector.parse_rss',return_value=(wrong,rejected)):
            self.ingest(headers={'etag':'"old"'})
        self.assertEqual(self.query()['news'],[])
        self.now += 900
        self.ingest(code=304)
        self.assertEqual(len(self.query()['news']),1)
        self.assertEqual(self.query(PUB+100)['news'],[])

    def test_retry_recovery_and_no_secret_logging(self):
        fetcher = Mock(side_effect=[URLError('secret-key-do-not-log'), (503, {}, b'secret'), (200, {}, rss())])
        with self.assertLogs('btc.news', level='INFO') as logs:
            collect(self.db, dict(interval_seconds=900,feeds=[self.source]), fetcher, self.clock, Mock())
        self.assertNotIn('secret', ''.join(logs.output))
        self.assertEqual(self.sql('SELECT error FROM attempts ORDER BY id'), [('network_error',), ('http_error',), (None,)])
        self.assertEqual(self.sql('SELECT count(*) FROM versions'), [(1,)])

    def test_long_retry_after_persists_across_restarts(self):
        fetcher = Mock(return_value=(429, {'retry-after':'3600'}, b'not saved'))
        collect_feed(self.db, self.source, 900, fetcher, self.clock, Mock())
        self.now += 900
        other = Mock()
        result = collect_feed(self.db, self.source, 900, other, self.clock, Mock())
        self.assertEqual(result['status'], 'deferred')
        other.assert_not_called()
        self.assertEqual(self.sql('SELECT raw_body FROM runs'), [(None,), (None,)])

    def test_403_not_retried_and_other_feeds_continue(self):
        fetcher = Mock(side_effect=[(403, {}, b'forbidden'), (200, {}, rss())])
        second = dict(self.source, id='second')
        result = collect(self.db, dict(interval_seconds=900, feeds=[self.source,second]), fetcher, self.clock, Mock())
        self.assertEqual([r['status'] for r in result], ['failed','ok'])
        self.assertEqual(fetcher.call_count, 2)

    def test_blocked_coindesk_makes_no_request(self):
        self.source = self.config['feeds'][0]
        fetcher = Mock()
        result = collect_feed(self.db, self.source, 900, fetcher, self.clock, Mock())
        self.assertEqual(result['status'], 'blocked')
        fetcher.assert_not_called()

    def test_policy_metadata_is_historical_and_not_current(self):
        self.ingest()
        self.now += 900
        self.source['policy']['model_input'] = False
        self.source['attribution'] = 'Changed'
        self.ingest(rss('Later'))
        self.assertEqual(self.query()['excluded']['policy'], 1)
        self.assertEqual(self.query(PUB+100)['news'][0]['attribution'], 'Board of Governors of the Federal Reserve System')

    def test_invalid_and_empty_feeds_visible_not_silent(self):
        self.assertEqual(self.ingest(b'<html>denied</html>')['status'], 'failed')
        self.assertEqual(self.ingest(b'<rss>broken')['status'], 'failed')
        self.assertEqual(self.ingest(b'<rss><channel/></rss>')['entries'], 0)
        self.assertEqual(self.sql('SELECT count(*) FROM versions'), [(0,)])

    def test_health_gaps_failures_and_historical_consistency(self):
        self.ingest()
        initial = store.health(self.db, self.now)
        self.now += 4000
        self.ingest(code=503)
        current = store.health(self.db, self.now)[0]
        self.assertTrue(current['stale'])
        self.assertTrue(current['gaps'])
        self.assertEqual(current['failed_runs'], 1)
        self.assertEqual(store.health(self.db, PUB+100), initial)

    def test_interrupted_runs_and_lock(self):
        store.start_run(self.db, self.source, 900, self.clock)
        self.assertEqual(store.health(self.db, self.now)[0]['interrupted_or_running'], 1)
        with collection_lock(self.db):
            with self.assertRaisesRegex(ValueError, 'already_running'):
                with collection_lock(self.db):
                    pass

    def test_clock_reversal_rolls_back(self):
        self.ingest()
        self.now -= 1
        with self.assertRaisesRegex(ValueError, 'clock_moved'):
            self.ingest(rss('Revision'))
        self.assertEqual(self.sql('SELECT count(*) FROM versions'), [(1,)])

    def test_separate_existing_db_and_readonly_queries(self):
        experiment = self.db.parent/'btc.db'
        with sqlite3.connect(experiment) as c:
            c.execute('CREATE TABLE secret(value TEXT)')
        before = experiment.read_bytes()
        with self.assertRaises(ValueError):
            store.initialize(experiment)
        renamed = self.db.parent/'unknown.sqlite'
        renamed.write_bytes(before)
        with self.assertRaises(ValueError):
            store.initialize(renamed)
        self.ingest()
        self.query()
        self.assertEqual(experiment.read_bytes(), before)
        missing = self.db.parent/'missing.db'
        with self.assertRaises(sqlite3.Error):
            store.snapshot(missing, self.now)
        self.assertFalse(missing.exists())

    def test_cli_snapshot_and_health(self):
        self.ingest()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--db',str(self.db),'snapshot','--as-of',str(self.now)]),0)
        self.assertEqual(json.loads(output.getvalue())['news'][0]['title'], 'Original')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--db',str(self.db),'health','--as-of',str(self.now),'--format','text']),0)
        self.assertIn('frb_press',output.getvalue())


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.source = load_config(ROOT/'news.config.example.json')['feeds'][1]

    def test_dates_timezone_and_url_validation(self):
        self.assertEqual(timestamp('Tue, 14 Nov 2023 17:13:20 -0500'), PUB)
        self.assertEqual(timestamp('2023-11-14T22:13:20Z'), PUB)
        self.assertEqual(timestamp('2023-11-14T17:13:20.2-05:00'), PUB)
        self.assertIsNone(timestamp('2023-11-14 22:13:20'))
        for url in ['file:///etc/passwd', 'https://user:secret@example.org/', 'https://example.org/?api_key=secret']:
            with self.assertRaises(ValueError):
                canonical_url(url)

    def test_atom_and_no_body_text(self):
        body = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>x</id><title>Atom</title><link href="https://example.org/a"/><published>2023-11-14T22:13:20Z</published><summary>brief</summary><content>BODY NEVER USED</content></entry></feed>'
        entries, rejected = parse_rss(body, self.source)
        self.assertEqual(entries[0]['item']['summary'], 'brief')
        self.assertNotIn('BODY',json.dumps(entries[0]['item']))
        self.assertIn('BODY', entries[0]['raw'])  # raw RSS allowed by government policy
        self.assertEqual(rejected,0)

    def test_xml_safety_and_rejections(self):
        for body in [b'<!DOCTYPE rss [<!ENTITY x "hi">]><rss/>',b'\x00<rss/>',b'a'*(MAX_BYTES+1)]:
            with self.assertRaises(ValueError):
                parse_rss(body,self.source)
        self.assertEqual(parse_rss(rss(url='invalid'),self.source),([],1))

    def test_retry_after(self):
        self.assertEqual(retry_delay('Tue, 14 Nov 2023 22:14:20 GMT',PUB),60)
        self.assertEqual(retry_delay('invalid',PUB),0)

    def test_bad_configuration(self):
        config = load_config(ROOT/'news.config.example.json')
        for mutate in [lambda c:c.update(interval_seconds=60),
                       lambda c:c['feeds'][0].update(enabled=True),
                       lambda c:c['feeds'][1].update(url='https://example.org/?token=secret')]:
            broken = copy.deepcopy(config)
            mutate(broken)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'config.json'
                path.write_text(json.dumps(broken))
                with self.assertRaises(ValueError):
                    load_config(path)


if __name__ == '__main__':
    unittest.main()
