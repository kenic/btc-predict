"""Claude routes expose only public read-only observations and matched comparisons."""
from contextlib import ExitStack
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import claude_observation as observation
import claude_views as views
import test_claude_observation as fixtures


class ClaudeViewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ClaudeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source=self.fixture.source
        self.side=self.fixture.side
        # The production side DB is next to the original DB, under its own name.
        self.side.rename(self.source.with_name('claude-observation.db'))
        self.side=self.source.with_name('claude-observation.db')
        self.fixture.side=self.side
        self.client=app.app.test_client()
        self.db_patch=patch.object(app,'DB_PATH',self.source)
        self.db_patch.start(); self.addCleanup(self.db_patch.stop)

    def snapshot(self):
        result=[]
        for path in (self.source,self.side):
            with sqlite3.connect(path) as c: result.append('\n'.join(c.iterdump()))
        return result

    def populate(self):
        f=self.fixture
        f.seed(); f.cycle()
        with observation.db(self.source) as c:
            c.execute("UPDATE phase6_events SET actual_direction='DOWN',actual_return=-2,evaluated_at='done'")
            for method,direction,p,correct,brier,pnl in (('gpt','DOWN',.2,1,.04,2),('logistic','UP',.6,0,.36,-2)):
                c.execute('INSERT INTO phase6_predictions VALUES(?,?,\'complete\',?,?,?,?,?,?)',
                          (f.t,method,direction,p,1-p,correct,brier,pnl))
            c.execute('UPDATE phase5b_predictions SET actual_rv=.4')
        with observation.db(self.side) as c:
            c.execute("UPDATE phase5c_predictions SET actual_rv=.4,evaluated_at='done',error='<unsafe>',raw_json='private raw',context='private context'")
            # Preserve snapshot matching for comparison, without leaking context via public output.
        with observation.db(self.source) as c:
            c.execute("UPDATE phase5b_predictions SET context='private context'")
        observation.evaluate(self.side,self.source,f.candles,f.t+3700)

    def test_missing_db_no_creation(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'DB_PATH',Path(directory)/'btc.db'):
            for section in ('phase5c','phase6c'):
                for url in ('/'+section+'/', '/'+section+'/data.json','/analyze/?phase='+section):
                    response=self.client.get(url)
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.headers['Cache-Control'],'no-store')
                self.assertEqual(self.client.get('/'+section+'/data.json').json['status'],'unavailable')
            self.assertFalse(list(Path(directory).iterdir()))

    def test_empty_schema_and_empty_registration(self):
        self.side.unlink()
        with sqlite3.connect(self.side): pass
        self.assertEqual(views.report(self.source)['status'],'awaiting_init')
        observation.migrate(self.side)
        self.assertEqual(views.report(self.source)['status'],'awaiting_init')
        self.assertIn('has not been initialized',self.client.get('/phase6c/').get_data(as_text=True))

    def test_registered_no_observations_lifecycle(self):
        data=views.report(self.source,clock=self.fixture.t-1)
        self.assertEqual(data['lifecycle'],'scheduled')
        self.assertEqual(data['planned'],3)
        self.assertEqual(data['recorded'],0)
        self.assertIn('Awaiting the first scheduled target',self.client.get('/phase5c/').get_data(as_text=True))
        self.assertIn('Awaiting an accepted ACTIVE target',self.client.get('/phase6c/').get_data(as_text=True))
        self.assertEqual(views.report(self.source,clock=observation.END)['lifecycle'],'window_closed')

    def test_populated_pages_and_json_are_read_only_no_provider(self):
        self.populate()
        before=self.snapshot()
        with ExitStack() as stack:
            for name in ('initialize','migrate','cycle','provider','evaluate'):
                stack.enter_context(patch.object(observation,name,side_effect=AssertionError('No experiment mutation from a page')))
            stack.enter_context(patch('urllib.request.build_opener',side_effect=AssertionError('No network from a page')))
            for section in ('phase5c','phase6c'):
                for url,active in (('/'+section+'/', '/'+section+'/'),('/analyze/?phase='+section,'/analyze/')):
                    response=self.client.get(url)
                    self.assertEqual(response.status_code,200)
                    text=response.get_data(as_text=True)
                    self.assertIn(f'href="{active}" aria-current="page"',text)
                    self.assertIn('claude-sonnet-4-6',text)
                    self.assertNotIn('private raw',text)
                    self.assertNotIn('private context',text)
                    self.assertNotIn('<unsafe>',text)
                    self.assertEqual(response.headers['Cache-Control'],'no-store')
                serialized=self.client.get('/'+section+'/data.json').get_data(as_text=True)
                self.assertNotIn('private raw',serialized)
                self.assertNotIn('private context',serialized)
                self.assertNotIn('MARKET DATA',serialized)
                self.assertNotIn('config_json',serialized)
        self.assertEqual(self.snapshot(),before)
        rv=self.client.get('/phase5c/data.json').json
        self.assertEqual(rv['rv_counts'],{'complete':1})
        self.assertEqual(rv['comparison']['rv']['gpt']['n'],1)
        self.assertAlmostEqual(rv['comparison']['rv']['claude']['mae'],.1)
        self.assertIn('&lt;unsafe&gt;',self.client.get('/phase5c/').get_data(as_text=True))
        direction=self.client.get('/phase6c/data.json').json
        self.assertEqual(direction['comparison']['direction']['common_target_n'],1)
        self.assertAlmostEqual(direction['recent'][0]['brier'],.49)
        self.assertEqual(direction['recent'][0]['agreement'],1)
        self.assertEqual(direction['comparison']['same_different']['different']['n'],1)
        self.assertIn('100% agreement',self.client.get('/phase6c/').get_data(as_text=True))

    def test_missing_source_retains_observation_progress(self):
        self.populate(); self.source.unlink()
        for section in ('phase5c','phase6c'):
            data=views.report(self.source,section)
            self.assertEqual(data['status'],'ready')
            self.assertEqual(data['comparison_status'],'unavailable')
            self.assertIsNone(data['comparison'])
            self.assertIn('Comparison data is currently unavailable',self.client.get('/'+section+'/').get_data(as_text=True))
        self.assertFalse(self.source.exists())

    def test_repeat_tie_partial_and_both_cohorts(self):
        f=self.fixture
        f.seed(); f.cycle()
        with observation.db(self.side) as c:
            c.execute("UPDATE phase6c_calls SET direction='DOWN' WHERE slot BETWEEN 1 AND 5")
        data=views.report(self.source,'phase6c')
        self.assertTrue(data['recent'][0]['tie'])
        self.assertIsNone(data['recent'][0]['modal_direction'])
        self.assertEqual(data['recent'][0]['agreement'],.5)
        f.seed(cohort='phase6b',target=f.t+3600); f.cycle(clock=f.t+3600+150)
        with observation.db(self.side) as c:
            c.execute("UPDATE phase6c_calls SET status='failed' WHERE cohort='phase6b' AND slot=10")
        data=views.report(self.source,'phase6c')
        self.assertEqual(data['accepted_targets'],2)
        self.assertEqual(data['complete_repeat_sets'],1)
        self.assertEqual(data['recent'][0]['cohort'],'phase6b')
        self.assertEqual(data['recent'][0]['successful_repeats'],9)
        self.assertIsNone(data['recent'][0]['agreement'])
        self.assertIn('Tie',self.client.get('/phase6c/').get_data(as_text=True))

    def test_pending_actuals_after_calendar_end_and_busy_store(self):
        self.fixture.seed(); self.fixture.cycle()
        self.assertEqual(views.report(self.source,clock=observation.END)['lifecycle'],'recovering_actuals')
        with sqlite3.connect(self.side) as c:
            c.execute('BEGIN EXCLUSIVE')
            self.assertEqual(views.report(self.source)['status'],'unavailable')

    def test_navigation_and_analysis_filters(self):
        from ui import page_shell,analysis_filters
        for section,label in (('phase5c','Phase 5c'),('phase6c','Phase 6c')):
            self.assertIn(f'href="/{section}/">{label}',page_shell('title','body','current'))
            self.assertIn(f'phase={section}" aria-current="page"',analysis_filters(section))


if __name__=='__main__': unittest.main()
