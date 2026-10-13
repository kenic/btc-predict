"""Phase 5b routes: exact JOIN, escaped HTML, and read-only incomplete states."""
from contextlib import ExitStack
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import phase5b
import phase5b_views
import phase6


class Phase5bViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'btc.db'
        self.client = app.app.test_client()
        self.db_patch = patch.object(app, 'DB_PATH', self.path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    def dump(self):
        with sqlite3.connect(self.path) as c:
            return '\n'.join(c.iterdump())

    def populate(self, with_phase6=True):
        with patch.object(phase5b, 'specification', return_value={'version':'phase5b-v1'}):
            phase5b.initialize(self.path, clock=3600)
        with sqlite3.connect(self.path) as c:
            for i, status in enumerate(('complete', 'complete', 'failed', 'missed')):
                c.execute('''INSERT INTO phase5b_predictions
                    (target_candle_time,started_at,status,version,predicted_rv,previous_rv,actual_rv,evaluated_at,error,context,raw_json)
                    VALUES(?,'now',?,'phase5b-v1',?,.2,?, ?, ?, 'secret context','secret raw')''',
                    (7200+i*3600, status, .1+i*.1 if status=='complete' else None,
                     .3 if i==0 else None, 'now' if i==0 else None,
                     '<script>alert(1)</script>' if i==2 else None))
        if with_phase6:
            with patch.object(phase6, 'DB_PATH', self.path):
                phase6.migrate()
            with sqlite3.connect(self.path) as c:
                # Event for a different hour must not affect a 5b target's outcome.
                for target in (7200, 10800, 999*3600):
                    c.execute("INSERT INTO phase6_events(target,started_at,gate,actual_return) VALUES(?,'now','accepted',2)", (target,))
                    for method, correct, pnl in (('gpt',1,2), ('logistic',0,-2)):
                        c.execute('''INSERT INTO phase6_predictions
                            (target,method,status,started_at,version,correct,signed_pnl)
                            VALUES(?,?,'complete','now','v',?,?)''', (target,method,correct,pnl))

    def test_missing_db_does_not_create_file(self):
        for url in ('/phase5b/', '/phase5b/data.json', '/analyze/?phase=phase5b'):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.client.get('/phase5b/data.json').json['status'], 'unavailable')

    def test_uninitialized_and_empty_config(self):
        phase5b.migrate(self.path)
        before = self.dump()
        response = self.client.get('/phase5b/')
        self.assertIn('has not been initialized', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/phase5b/data.json').json['status'], 'awaiting_init')
        self.assertEqual(before, self.dump())

    def test_populated_routes_and_no_writes_or_provider_calls(self):
        self.populate()
        before = self.dump()
        with ExitStack() as stack:
            for name in ('initialize', 'run', 'evaluate', 'migrate'):
                stack.enter_context(patch.object(phase5b, name, side_effect=AssertionError('Unexpected experiment mutation')))
            stack.enter_context(patch('requests.get', side_effect=AssertionError('Unexpected network call')))
            for url, active in (('/phase5b/', '/phase5b/'), ('/analyze/?phase=phase5b', '/analyze/')):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                text = response.get_data(as_text=True)
                self.assertIn(f'href="{active}" aria-current="page"', text)
                self.assertIn('500', text)
                self.assertIn('Common scored targets', text)
                self.assertIn('100.0000', text)
                self.assertIn('&lt;script&gt;', text)
                self.assertNotIn('<script>alert', text)
                self.assertNotIn('secret context', text)
                self.assertNotIn('secret raw', text)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            data = self.client.get('/phase5b/data.json').json
        self.assertEqual(before, self.dump())
        self.assertEqual(data['recorded'], 4)
        self.assertEqual(data['remaining'], 496)
        self.assertEqual(data['counts'], {'complete':2, 'failed':1, 'missed':1})
        self.assertEqual(data['actuals_available'], 1)
        self.assertEqual(data['actuals_pending'], 3)
        self.assertEqual(data['recent'][0]['target_candle_time'], 18000)
        self.assertEqual(data['comparison']['percentile_population'], 2)
        self.assertEqual(sum(g['paired_N'] for g in data['comparison']['bins']), 2)

    def test_no_phase6_schema_retains_progress(self):
        self.populate(with_phase6=False)
        data = self.client.get('/phase5b/data.json').json
        self.assertFalse(data['phase6_available'])
        self.assertEqual(data['comparison']['percentile_population'], 2)
        self.assertEqual(sum(g['paired_N'] for g in data['comparison']['bins']), 0)
        self.assertIn('comparison data is not available', self.client.get('/phase5b/').get_data(as_text=True))

    def test_empty_registered_window_and_busy_db(self):
        with patch.object(phase5b, 'specification', return_value={'version':'phase5b-v1'}):
            phase5b.initialize(self.path, clock=3600)
        self.assertIn('Awaiting the first scheduled target', self.client.get('/phase5b/').get_data(as_text=True))
        with sqlite3.connect(self.path) as c:
            c.execute('BEGIN EXCLUSIVE')
            data = phase5b_views.report(self.path)
            self.assertEqual(data['status'], 'unavailable')

    def test_json_route_navigation_on_existing_page(self):
        with patch('regression_views.page', return_value='fixture'):
            self.assertEqual(self.client.get('/regression/').status_code, 200)
        from ui import page_shell, analysis_filters
        self.assertIn('href="/phase5b/">Phase 5b', page_shell('title','content','current'))
        self.assertIn('phase=phase5b" aria-current="page"', analysis_filters('phase5b'))


if __name__ == '__main__':
    unittest.main()
