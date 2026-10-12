import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from active_analysis import METHODS, compare, fit, metrics, report, render, timestamp
from phase6_methods import LOGISTIC_KEYS
from volatility import classify

class ActiveAnalysisTests(unittest.TestCase):
    def rows(self):
        return [dict(target=i*3600,available_at=(i+1)*3600+1,actual=('QUIET','NORMAL','ACTIVE')[i%3],features=dict.fromkeys(LOGISTIC_KEYS,float(i%3)),gpt=dict(active=i%3==2,probability=None)) for i in range(105)]

    def test_past_only_both_fits(self):
        rows=self.rows(); rows[0]['available_at']=math.inf
        for binary in (False,True):
            prediction,audit=fit(rows[-1]['features'],rows,104*3600,binary)
            self.assertIsNotNone(prediction)
            self.assertEqual(audit['targets'],[i*3600 for i in range(1,103)])
            altered=rows+[dict(rows[-1],target=999*3600)]
            self.assertEqual(fit(rows[-1]['features'],altered,104*3600,binary)[0],prediction)

    def test_counts_and_missing_probability(self):
        s=metrics([(True,dict(active=True,probability=.8)),(False,dict(active=True,probability=None)),(True,dict(active=False,probability=float('nan'))),(False,dict(active=False))])
        self.assertEqual([s[k] for k in ('TP','FP','FN','TN')],[1,1,1,1])
        self.assertEqual((s['recall'],s['precision'],s['accuracy']),(.5,.5,.5))
        self.assertEqual(s['brier_n'],1); self.assertAlmostEqual(s['brier'],.04)
        self.assertIsNone(metrics([])['recall'])

    def test_intersection(self):
        rows=self.rows(); rows[-1]['gpt']=None
        result=compare(rows)
        self.assertEqual(result['timestamps'],[101*3600,102*3600,103*3600])
        self.assertEqual(result['common_n'],3)
        self.assertEqual(result['actual_active'],1)
        self.assertEqual(set(result['methods']),set(METHODS))

    def test_mapping_frozen_threshold_readonly_and_render(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'test.db'; config=dict(quiet_upper=.2,active_lower=.4)
            with sqlite3.connect(path) as c:
                c.executescript('CREATE TABLE phase4_configs(config_id TEXT,config_json TEXT); CREATE TABLE volatility_predictions(id INTEGER,target_candle_time INTEGER,predictor TEXT,config_id TEXT,context TEXT,p_quiet REAL,p_normal REAL,p_active REAL,actual_rv REAL,evaluated_at TEXT);')
                c.execute('INSERT INTO phase4_configs VALUES (?,?)',('frozen',json.dumps(config)))
                for i,p in enumerate(((.7,.2,.1),(.1,.8,.1),(.1,.2,.7))):
                    c.execute('INSERT INTO volatility_predictions VALUES (?,?,?,?,?,?,?,?,?,?)',(i,3600*(i+1),'openai','frozen','invalid',*p,.4,'2020-01-01T00:00:00+00:00'))
            before=path.read_bytes(); captured=[]
            def fake(rows):
                captured.extend(rows); return compare([])
            with patch('active_analysis.compare',side_effect=fake): report(path)
            self.assertEqual([r['gpt']['active'] for r in captured],[False,False,True])
            self.assertEqual([r['actual'] for r in captured],['ACTIVE']*3)
            self.assertEqual(classify(.4,config),'ACTIVE')
            self.assertIn('ACTIVE detector comparison',render(path))
            self.assertEqual(path.read_bytes(),before)

    def test_missing_db_and_ambiguous_availability(self):
        self.assertEqual(report('/missing-active-analysis-db')['common_n'],0)
        self.assertEqual(timestamp('2020-01-01T00:00:00'),math.inf)

    def test_three_way_intersection_and_actual_required(self):
        rows=self.rows()[-4:]
        rows[2]['actual']=None
        def fake(features,training,cutoff,binary=False):
            # A misses the first target; B misses the second target.
            if cutoff==rows[int(binary)]['target']:
                return None,{}
            return dict(active=False,probability=None),{}
        with patch('active_analysis.fit',side_effect=fake):
            self.assertEqual(compare(rows)['timestamps'],[rows[3]['target']])

    def test_no_new_provider_calls_or_live_rows_and_route(self):
        import app
        import db
        import phase4
        import phase6
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
            path=Path(d)/'btc.db'
            for module in (app,db,phase4,phase6):
                stack.enter_context(patch.object(module,'DB_PATH',path))
            db.init_db(); phase4.init_schema(); phase6.migrate()
            with sqlite3.connect(path) as c:
                before='\n'.join(c.iterdump())
            for target in ('requests.get','phase6.direction_call','next_stages.class_call','phase6.run','openai.resources.responses.Responses.create'):
                stack.enter_context(patch(target,side_effect=AssertionError('Analysis must not call providers or live prediction')))
            response=app.app.test_client().get('/analyze/?phase=phase6')
            self.assertEqual(response.status_code,200)
            self.assertIn('ACTIVE detector comparison',response.get_data(as_text=True))
            with sqlite3.connect(path) as c:
                self.assertEqual(before,'\n'.join(c.iterdump()))

    def test_missing_features_and_minimum_classes(self):
        rows=self.rows()
        for binary in (False,True):
            self.assertIsNone(fit({},rows,999*3600,binary)[0])
            self.assertIsNone(fit(rows[0]['features'],[dict(r,actual='QUIET') for r in rows],999*3600,binary)[0])

    def test_populated_report_expanding_window(self):
        import json
        from datetime import datetime,timezone
        from test_phase6 import snapshot
        import phase4
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'btc.db'
            with patch.object(phase4,'DB_PATH',path): phase4.init_schema()
            config=dict(quiet_upper=.2,active_lower=.4)
            with sqlite3.connect(path) as c:
                c.execute('INSERT INTO phase4_configs VALUES (?,?)',('frozen',json.dumps(config)))
                for i in range(110):
                    t=(i+1)*3600; p=[.1,.1,.1]; p[i%3]=.8
                    at=datetime.fromtimestamp(t+3601,timezone.utc).isoformat()
                    c.execute('''INSERT INTO volatility_predictions(created_at,target_candle_time,predictor,model_version,config_id,p_quiet,p_normal,p_active,context,previous_rv,persistence_class,actual_rv,evaluated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',('fixture',t,'openai','fixture','frozen',*p,snapshot(t),.1,'QUIET',(.1,.3,.4)[i%3],at))
            before=path.read_bytes()
            with patch('requests.get',side_effect=AssertionError('No network')):
                r=report(path)
            self.assertEqual(r['common_n'],9)
            self.assertEqual(r['actual_active'],3)
            self.assertEqual([r['methods']['GPT'][k] for k in ('TP','FP','FN','TN')],[3,0,0,6])
            self.assertEqual(path.read_bytes(),before)
            for t,methods in r['audit'].items():
                for a in methods.values():
                    self.assertTrue(all(target+3600<t for target in a['targets']))
