"""Render every route against isolated databases without external API calls."""
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import app
import db
import phase4
import phase4_views
import next_stages
import next_stage_views


class SharedUITests(unittest.TestCase):
    def test_routes_navigation_and_read_only_state(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / 'btc.db'
            for module in (app, db, phase4, phase4_views, next_stages, next_stage_views):
                stack.enter_context(patch.object(module, 'DB_PATH', path))
            stack.enter_context(patch.object(app, 'get_btc_price', return_value=60000))
            stack.enter_context(patch.object(app, 'get_latest_microstructure', return_value={"windows": [], "status": "No fixture market data", "cutoff": 3600}))
            db.init_db()
            phase4.init_schema()
            next_stages.migrate()
            with sqlite3.connect(path) as connection:
                before = '\n'.join(connection.iterdump())
            client = app.app.test_client()
            for url, active in [('/', '/'), ('/direction/', '/direction/'),
                                ('/volatility/', '/volatility/'), ('/repeated/', '/repeated/'),
                                ('/regression/', '/regression/'), ('/next-stages/', '/repeated/')]:
                self.check_page(client, url, active)
            for phase in ('all','phase1','phase2','phase3','phase4','phase4r','phase5'):
                self.check_page(client, '/analyze/?phase='+phase, '/analyze/')
            self.check_page(client, '/analyze/', '/analyze/')
            for url in ('/static/style.css','/static/btc.png','/next-stages/data.json'):
                self.assertEqual(client.get(url).status_code, 200, url)
            with sqlite3.connect(path) as connection:
                self.assertEqual(before, '\n'.join(connection.iterdump()))

    def check_page(self, client, url, active):
        response = client.get(url)
        self.assertEqual(response.status_code, 200, url)
        rendered = response.get_data(as_text=True)
        self.assertIn('href="/static/style.css"', rendered)
        self.assertIn('href="/static/btc.png"', rendered)
        self.assertIn('aria-label="Main navigation"', rendered)
        self.assertIn(f'href="{active}" aria-current="page"', rendered)
        for _, route, label in __import__('ui').NAV:
            self.assertIn(f'>{label}</a>', rendered)
        self.assertNotIn('<style>', rendered)
        self.assertNotIn('border="1"', rendered)
        if url.startswith('/analyze/'):
            self.assertIn('aria-label="Analysis experiment"', rendered)
            for phase in ('phase4','phase4r','phase5'):
                self.assertIn('/analyze/?phase='+phase, rendered)

    def test_populated_later_stage_cards_and_diagnostics(self):
        data = dict(phase4r={'openai':dict(n=2,expected_runs=20,completed_runs=11,evaluated_n=1,
            single_brier=.4,ensemble_brier=.2,mean_dispersion=.01)}, paired_n=1,
            paired_evaluated_n=1,paired_scores={'openai':dict(single_brier=.4,ensemble_brier=.2)},
            diagnostics=[dict(source_id=1,completed=10)],failed_or_uncertain_runs=[dict(status='failed',error='<unsafe>')],
            transitions=[dict(stage='phase5')], phase5=dict(predicted_n=2,attempted_n=3,
            model=dict(n=1,mae=.2,rmse=.2,bias=-.2),previous_hour_baseline=dict(n=1,mae=.3,rmse=.3,bias=.3),
            predicted_vs_actual=[dict(target_candle_time=3600,predicted_rv=.1,actual_rv=.3,previous_rv=.6,model_version='fixture')],
            pending=[dict(target_candle_time=7200,status='failed',predicted_rv=None,error='<unsafe>')]))
        from repeated_analysis import summarize
        from test_repeated_analysis import diagnostic
        d = diagnostic(3600, [10,0,0])
        d.update(source_id=1,actual_class='QUIET')
        data['diagnostics'] = [d]
        data['phase4r']['openai'].update(summarize([d]))
        data['phase4r']['openai'].update(invalid_runs=0,processed_runs=11,excluded_invalid_snapshots=0)
        data.update(paired_stability_n=0,paired_stability={},retry_exceptions=[])
        with patch.object(next_stage_views,'report',return_value=data):
            repeated=next_stage_views.page(section='repeated')
            regression=next_stage_views.page(section='regression')
        self.assertIn('Single-shot Brier', repeated)
        self.assertIn('0.4000', repeated)
        self.assertIn('Completed runs', repeated)
        self.assertIn('MAE',regression)
        self.assertIn('-0.2000',regression)
        self.assertIn('Previous-hour RV baseline',regression)
        self.assertIn('Predicted vs actual',regression)
        for rendered in (repeated,regression):
            self.assertIn('&lt;unsafe&gt;',rendered)
            self.assertNotIn('<unsafe>',rendered)


if __name__ == '__main__':
    unittest.main()
