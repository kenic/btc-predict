"""Public archive reads against isolated schema-compatible SQLite fixtures."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import news_views as view

T = 1700100000
SCHEMA = '''
CREATE TABLE feeds(id TEXT PRIMARY KEY,first_configured_at REAL);
CREATE TABLE runs(id INTEGER PRIMARY KEY,feed_id TEXT,started_at REAL,available_at REAL,
 status TEXT,source_json TEXT,interval_seconds INTEGER,rejected INTEGER,http_status INTEGER,raw_body BLOB);
CREATE TABLE articles(id INTEGER PRIMARY KEY,feed_id TEXT,first_seen_at REAL,last_seen_at REAL);
CREATE TABLE versions(id INTEGER PRIMARY KEY,article_id INTEGER,run_id INTEGER,available_at REAL,item_json TEXT,raw_item TEXT);
CREATE TABLE observations(run_id INTEGER,article_id INTEGER,version_id INTEGER,observed_at REAL);
'''


class NewsPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'news.db'
        with sqlite3.connect(self.path) as c:
            c.executescript(SCHEMA)
            c.execute('PRAGMA application_id=%d' % view.APP_ID)
            c.execute('PRAGMA user_version=1')
        self.seed_run(1,'frb_press',T-900)
        self.article(1,1,'Original release',T-3600)
        self.seed_run(2,'bls_cpi',T-800)
        self.article(2,2,'Consumer prices',T-1800)
        self.seed_run(3,'coindesk',T-700,status='blocked',enabled=False)
        self.patcher=patch.object(app,'NEWS_DB_PATH',self.path)
        self.patcher.start()
        self.client=app.app.test_client()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    # Avoid overriding unittest.TestCase.run.
    def seed_run(self,ident,source,at,status='ok',enabled=True):
        config=dict(enabled=enabled,policy=dict(mode='rss' if enabled else 'blocked',model_input=True),
                    private_internal_setting='SECRET_CONFIG_SENTINEL')
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT OR IGNORE INTO feeds VALUES(?,?)',(source,at))
            c.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (ident,source,at-1,at,status,json.dumps(config),900,0,200,b'SECRET_RAW_SENTINEL'))

    def article(self,ident,run_id,title,published,url=None,article_id=None):
        with sqlite3.connect(self.path) as c:
            source,at=c.execute('SELECT feed_id,available_at FROM runs WHERE id=?',(run_id,)).fetchone()
            aid=ident if article_id is None else article_id
            c.execute('INSERT OR IGNORE INTO articles VALUES(?,?,?,?)',(aid,source,at,at))
            c.execute('UPDATE articles SET last_seen_at=? WHERE id=?',(at,aid))
            item=dict(title=title,summary='RSS summary <script>alert(1)</script>',published_at=published,
                      url=url or 'https://www.'+view.PUBLIC_HOSTS.get(source,'coindesk.com')+'/release/'+str(aid),
                      attribution='Official source',private_field='SECRET_ITEM_SENTINEL')
            c.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)',
                      (ident,aid,run_id,at,json.dumps(item),'SECRET_RAW_ITEM_SENTINEL'))
            c.execute('INSERT INTO observations VALUES(?,?,?,?)',(run_id,aid,ident,at))

    def query(self,cutoff=T,source='all'):
        return view.report(self.path,cutoff,source)

    def test_page_navigation_form_attribution_and_no_raw_exposure(self):
        response=self.client.get('/phase8pre/?as_of='+str(T))
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        for text in ('Phase 8pre','Source status','Polling coverage','Acquisition history','Permission pending',
                     'name="as_of"','name="source"','Official source','href="/phase8pre/" aria-current="page"'):
            self.assertIn(text,html)
        self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('<script>',html)
        self.assertNotIn('SECRET_',html)
        self.assertNotIn('http-equiv="refresh"',html)
        self.assertEqual(response.headers['Cache-Control'],'no-store')

    def test_latest_refreshes_but_fixed_cutoff_does_not(self):
        with patch('time.time',return_value=T):
            html=self.client.get('/phase8pre/').get_data(as_text=True)
        self.assertIn('http-equiv="refresh" content="60"',html)

    def test_json_is_only_public_projection(self):
        response=self.client.get('/phase8pre/data.json?as_of='+str(T))
        self.assertEqual(response.status_code,200)
        data=response.get_json()
        self.assertEqual(data['eligible_count'],2)
        self.assertEqual(data['archived_articles'],2)
        self.assertEqual(data['archived_versions'],2)
        self.assertNotIn('SECRET_',response.get_data(as_text=True))
        self.assertNotIn(str(self.path),response.get_data(as_text=True))
        self.assertEqual(response.headers['Cache-Control'],'no-store')

    def test_past_snapshot_unchanged_by_future_revision(self):
        before=self.query()
        self.seed_run(4,'frb_press',T+100)
        self.article(4,4,'Later corrected release',T-3600,article_id=1)
        self.seed_run(5,'bls_cpi',T+200)
        self.article(5,5,'Late arrival',T-1200)
        self.assertEqual(self.query(),before)
        later=self.query(T+200)
        self.assertIn('Later corrected release',[n['title'] for n in later['news']])
        self.assertEqual(next(n for n in before['news'] if n['article_id']==1)['last_fetched_at'],T-900)

    def test_latest_revision_filtered_before_publication_window(self):
        self.seed_run(4,'frb_press',T-50)
        self.article(4,4,'Corrected to tomorrow',T+86400,article_id=1)
        self.assertNotIn(1,[n['article_id'] for n in self.query()['news']])
        self.assertNotIn(1,[n['article_id'] for n in self.query()['recent']])

    def test_publication_boundaries_and_before_start(self):
        self.article(4,1,'Exactly lower boundary',T-86400)
        self.article(5,1,'Exactly upper boundary',T)
        self.article(6,1,'No publication date',None)
        names=[n['title'] for n in self.query()['news']]
        self.assertNotIn('Exactly lower boundary',names)
        self.assertIn('Exactly upper boundary',names)
        self.assertNotIn('No publication date',names)
        before=self.query(T-10000)
        self.assertEqual(before['sources'],[])
        self.assertEqual(before['archived_articles'],0)

    def test_source_filter_and_canonical_duplicate_handling(self):
        self.article(4,1,'Same article duplicate',T-3600,url='https://www.federalreserve.gov/release/1')
        self.assertEqual(len(self.query()['news']),2)
        filtered=self.query(source='bls_cpi')
        self.assertEqual([n['title'] for n in filtered['news']],['Consumer prices'])

    def test_private_or_unreviewed_sources_and_unsafe_links_hidden(self):
        self.seed_run(4,'private_partner',T-50,enabled=True)
        self.article(4,4,'PRIVATE_LICENSED_TITLE',T-100)
        for i,url in enumerate(('javascript:alert(1)','https://user:password@bls.gov/a',
                                'https://bls.gov.evil.example/a','https://evil.example/a'),5):
            self.article(i,2,'UNSAFE_TITLE',T-100,url=url)
        data=self.query()
        rendered=view.render(data)
        self.assertNotIn('PRIVATE_LICENSED_TITLE',rendered+json.dumps(data))
        self.assertNotIn('UNSAFE_TITLE',rendered+json.dumps(data))
        self.assertIn('private_partner',[s['id'] for s in data['sources']])

    def test_paused_is_not_counted_as_outage(self):
        data=self.query()
        paused=next(s for s in data['sources'] if s['id']=='coindesk')
        self.assertEqual(paused['status'],'paused')
        self.assertEqual(paused['gaps'],[])
        self.assertTrue(all(b['state'] in ('paused','before') for b in paused['bins']))

    def test_stale_source_failure_and_polling_gap(self):
        self.seed_run(4,'frb_press',T+1800,status='failed')
        source=next(s for s in self.query(T+3600)['sources'] if s['id']=='frb_press')
        self.assertEqual(source['status'],'attention')
        self.assertEqual(source['failed_polls'],1)
        self.assertTrue(source['gaps'])
        self.assertEqual(len(source['bins']),96)

    def test_readonly_and_no_network(self):
        before=self.path.read_bytes()
        with patch('socket.create_connection',side_effect=AssertionError('network from view')):
            self.assertEqual(self.client.get('/phase8pre/?as_of='+str(T)).status_code,200)
            self.assertEqual(self.client.get('/phase8pre/data.json?as_of='+str(T)).status_code,200)
        self.assertEqual(before,self.path.read_bytes())

    def test_missing_corrupt_and_foreign_db_fail_cleanly(self):
        missing=Path(self.tmp.name)/'does-not-exist.db'
        corrupt=Path(self.tmp.name)/'corrupt.db'
        corrupt.write_bytes(b'SECRET_CORRUPT_DB')
        foreign=Path(self.tmp.name)/'foreign.db'
        with sqlite3.connect(foreign) as c:
            c.execute('CREATE TABLE private(value TEXT)')
        for path in (missing,corrupt,foreign):
            with patch.object(app,'NEWS_DB_PATH',path):
                r=self.client.get('/phase8pre/')
                self.assertEqual(r.status_code,503)
                self.assertIn('News archive unavailable',r.get_data(as_text=True))
                self.assertNotIn('SECRET_',r.get_data(as_text=True))
        self.assertFalse(missing.exists())

    def test_invalid_requests_are_400(self):
        for query in ('as_of=NaN','as_of=Infinity','as_of=-1','as_of=invalid',
                      'as_of=9999999999999','source=unknown'):
            for route in ('/phase8pre/','/phase8pre/data.json'):
                self.assertEqual(self.client.get(route+'?'+query).status_code,400,query)

    def test_timezones_and_datetime_form(self):
        a=view.parse_cutoff('2026-11-11T17:00:00+09:00',2000000000)
        self.assertEqual(a,view.parse_cutoff('2026-11-11T08:00:00Z',2000000000))
        self.assertEqual(a,view.parse_cutoff('2026-11-11T08:00',2000000000))

    def test_no_news_window_explains_sparse_releases_and_shows_archive(self):
        html=view.render(self.query(T+86400))
        self.assertIn('No eligible news',html)
        self.assertIn('Recent archived headlines',html)
        self.assertIn('Outside selected 24h',html)

    def test_list_is_bounded(self):
        for i in range(10,130):
            self.article(i,1,'Release '+str(i),T-100-i)
        data=self.query()
        self.assertEqual(len(data['news']),100)
        self.assertTrue(data['truncated'])

    def test_operator_enabled_coindesk_rss_visible_only_after_acquisition(self):
        before=self.query()
        self.seed_run(4,'coindesk',T+20,enabled=True)
        self.article(4,4,'Bitcoin news from CoinDesk RSS',T-300)
        self.assertEqual(before,self.query())
        later=self.query(T+30,source='coindesk')
        self.assertEqual([n['title'] for n in later['news']],['Bitcoin news from CoinDesk RSS'])
        self.assertIn('coindesk.com',later['news'][0]['url'])
        source=next(s for s in later['sources'] if s['id']=='coindesk')
        self.assertEqual(source['gaps'],[])
        self.assertNotIn('unobserved',[b['state'] for b in source['bins']])


if __name__=='__main__':
    unittest.main()
