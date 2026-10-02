import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import volatility as v
import phase4

class Phase4Tests(unittest.TestCase):
    def test_exact_close_to_close_rv(self):
        t=3600
        candles=[[t-300+i*300,0,0,0,100*math.exp(i*.001),0] for i in range(13)]
        self.assertAlmostEqual(v.realized_volatility(candles,t),100*math.sqrt(12)*.001)
        with self.assertRaises(ValueError):
            v.realized_volatility(candles[1:],t)
        with self.assertRaises(ValueError):
            v.realized_volatility(candles[:5]+candles[6:],t)
        candles[4][4]=float('nan')
        with self.assertRaises(ValueError):
            v.realized_volatility(candles,t)

    def test_boundaries_probabilities_and_score(self):
        config={'quiet_upper':.2,'active_lower':.4}
        self.assertEqual(v.classify(.2,config),'NORMAL')
        self.assertEqual(v.classify(.4,config),'ACTIVE')
        self.assertEqual(v.winner([1/3]*3),'QUIET')
        self.assertEqual(v.brier([0,0,1],'ACTIVE'),0)
        self.assertEqual(v.brier([1,0,0],'ACTIVE'),2)
        self.assertAlmostEqual(v.brier([1/3]*3,'ACTIVE'),2/3)
        for p in ([float('nan'),0,1],[.5,.5,.5],[-1,1,1]):
            with self.assertRaises(ValueError): v.probabilities(p)

    def test_additive_migration_and_closure_gate(self):
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'btc.db'
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE predictions(candle_time INTEGER,predictor TEXT,phase TEXT,evaluated_at TEXT)')
                c.executemany('INSERT INTO predictions VALUES (?,?,?,?)',[(i,m,'phase3','done' if i<47 else None) for i in range(48) for m in ['jev','openai']])
                original=c.execute('SELECT * FROM predictions').fetchall()
            with patch.object(phase4,'DB_PATH',db):
                phase4.init_schema();phase4.init_schema()
            with sqlite3.connect(db) as c:
                self.assertEqual(original,c.execute('SELECT * FROM predictions').fetchall())
                with self.assertRaises(RuntimeError):phase4.assert_phase3_closed(c)
                c.execute("UPDATE predictions SET evaluated_at='done'")
                phase4.assert_phase3_closed(c)
                c.execute("INSERT INTO phase4_configs VALUES ('id','{}')")
                with self.assertRaises(sqlite3.IntegrityError):c.execute("UPDATE phase4_configs SET config_json='x'")
                with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM phase4_configs')

    def test_frozen_history(self):
        import hashlib
        from volatility import quantile
        config=json.loads(phase4.CONFIG_PATH.read_text())
        path=phase4.ROOT/'phase4_history_5m.json'
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),config['history_sha256'])
        candles=json.loads(path.read_text())
        self.assertLess(max(c[0] for c in candles),config['calibration_end'])
        values=[v.realized_volatility(candles,t) for t in range(config['calibration_start'],config['calibration_end'],3600)]
        self.assertEqual(len(values),336)
        self.assertEqual(quantile(values,1/3),config['quiet_upper'])
        self.assertEqual(quantile(values,2/3),config['active_lower'])

    def test_prediction_guard_with_pending_phase3(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'btc.db'
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE predictions(candle_time INTEGER,predictor TEXT,phase TEXT,evaluated_at TEXT)')
                c.execute("INSERT INTO predictions VALUES (0,'openai','phase3',NULL)")
            with patch.object(phase4,'DB_PATH',db):
                with self.assertRaisesRegex(RuntimeError,'Phase 3'):
                    phase4.load_config(require_enabled=True)


class EvaluationAndViewsTests(unittest.TestCase):
    def test_evaluation_and_views_with_real_schema(self):
        import evaluate_volatility as ev
        import phase4_views as views
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'btc.db'
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE predictions(candle_time INTEGER,predictor TEXT,phase TEXT,evaluated_at TEXT)')
                c.executemany('INSERT INTO predictions VALUES (?,?,?,?)',[(i,m,'phase3','done') for i in range(48) for m in ['jev','openai']])
            with patch.object(phase4,'DB_PATH',db):
                phase4.init_schema()
            config={'quiet_upper':.2,'active_lower':.4,'historical_hours':336,'majority_class':'QUIET'}
            with sqlite3.connect(db) as c:
                c.execute('INSERT INTO phase4_configs VALUES (?,?)',('test',json.dumps(config)))
                c.execute('INSERT INTO volatility_predictions(created_at,target_candle_time,predictor,model_version,config_id,p_quiet,p_normal,p_active,context,previous_rv,persistence_class,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                          ('now',3600,'openai','test','test',.8,.1,.1,'snapshot',.1,'QUIET','<script>bad</script>'))
            candles=[[3300+i*300,0,0,0,100,1] for i in range(13)]
            with patch.object(ev,'DB_PATH',db),patch.object(ev,'fetch_candles',return_value=candles[1:]):
                with self.assertRaises(RuntimeError):ev.evaluate_all()
            with sqlite3.connect(db) as c:
                self.assertIsNone(c.execute('SELECT evaluated_at FROM volatility_predictions').fetchone()[0])
            with patch.object(ev,'DB_PATH',db),patch.object(ev,'fetch_candles',return_value=candles):
                ev.evaluate_all(); ev.evaluate_all()
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT actual_class,correct FROM volatility_predictions').fetchone(),('QUIET',1))
                self.assertEqual(c.execute('SELECT count(*) FROM predictions').fetchone()[0],96)
            with patch.object(views,'DB_PATH',db):
                rendered=views.page(analysis=True)
                self.assertIn('0–10%',rendered)
                self.assertIn('confusion matrix',rendered)
                self.assertNotIn('<script>bad</script>',rendered)
                self.assertIn('&lt;script&gt;',rendered)

class PredictorIntegrationTests(unittest.TestCase):
    def test_both_models_use_three_classes_and_same_phase3_snapshot(self):
        import phase4_runner as runner
        import predict_gpt as source
        import indicators
        import microstructure
        import types
        import sys
        from unittest.mock import MagicMock
        cutoff=source.get_prediction_cutoff()
        config={'quiet_upper':.2,'active_lower':.4,'calibration_end':cutoff,'prediction_limit':48}
        client=MagicMock()
        client.responses.create.return_value.output_text=json.dumps(dict(p_quiet=.2,p_normal=.3,p_active=.5,reason='test'))
        answer=types.SimpleNamespace(probabilities={'QUIET':.2,'NORMAL':.3,'ACTIVE':.5},confidence=.3,choice='ACTIVE')
        jev=MagicMock()
        jev.system_one.return_value=types.SimpleNamespace(answers={'volatility':answer},model='jev-test')
        sdk=types.ModuleType('typesafe_sdk')
        sdk.TypeSafeClient=MagicMock(return_value=jev)
        sdk.Choice=MagicMock()
        def candles(g):
            return [[cutoff-g*i,0,0,0,100,1] for i in range(60,0,-1)]
        from datetime import datetime, timezone
        test_clock=MagicMock(wraps=datetime)
        test_clock.now.return_value=datetime.fromtimestamp(cutoff+60,timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'test.db'
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE volatility_predictions(target_candle_time INTEGER,predictor TEXT)')
                c.executemany('INSERT INTO volatility_predictions VALUES (?,?)',
                              [(cutoff-(i+1)*3600,m) for i in range(48) for m in ('openai','jev')])
            with patch.object(runner,'datetime',test_clock),patch.object(runner,'DB_PATH',db),patch.object(runner,'load_config',return_value=config),patch.object(runner,'save_prediction') as save,patch.object(source,'get_candles',side_effect=candles),patch.object(source,'validate_microstructure'),patch.object(indicators,'calculate_timeframe_indicators',return_value=None),patch.object(indicators,'make_multitimeframe_snapshot',return_value='Direction of the next completed 1-hour candle. technicals'),patch.object(microstructure,'make_microstructure_snapshot',return_value='orderbook tradeflow'),patch('openai.OpenAI',return_value=client),patch.dict(sys.modules,{'typesafe_sdk':sdk}),patch.dict('os.environ',{'TYPESAFE_API_KEY':'test'}):
                runner.run('openai'); runner.run('jev')
                self.assertEqual(save.call_count,2)
                gpt_args,jev_args=[call.args for call in save.call_args_list]
                self.assertEqual(gpt_args[4], [.2,.3,.5])
                self.assertEqual(jev_args[4], [.2,.3,.5])
                self.assertEqual(gpt_args[5],jev_args[5])
                self.assertIn('orderbook tradeflow',gpt_args[5])
                self.assertNotIn('Direction of',gpt_args[5])
                self.assertEqual(set(sdk.Choice.call_args.kwargs['criteria']),set(v.CLASSES))
                self.assertIn('squared',client.responses.create.call_args.kwargs['input'])
                with sqlite3.connect(db) as c:
                    c.executemany('INSERT INTO volatility_predictions VALUES (?,?)',
                                  [(cutoff-(i+49)*3600,m) for i in range(48) for m in ('openai','jev')])
                runner.run('openai'); runner.run('jev')
                self.assertEqual(save.call_count,2)
                self.assertEqual(client.responses.create.call_count,1)
                self.assertEqual(jev.system_one.call_count,1)

class ContinuationTests(unittest.TestCase):
    def test_concurrent_final_slot_and_consistent_backup(self):
        from concurrent.futures import ThreadPoolExecutor
        import migrate_phase4_continuation as migration
        config=json.loads(phase4.CONFIG_PATH.read_text())
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            db=root/'btc.db'
            (root/'phase4_config.json').write_text(json.dumps(config))
            with patch.object(phase4,'DB_PATH',db):
                phase4.init_schema()
                with sqlite3.connect(db) as c:
                    c.execute('CREATE TABLE predictions(candle_time INTEGER,predictor TEXT,phase TEXT,evaluated_at TEXT)')
                    c.executemany('INSERT INTO predictions VALUES (?,?,?,?)',[(i,m,'phase3','done') for i in range(48) for m in ('openai','jev')])
                    c.execute('INSERT INTO phase4_configs VALUES (?,?)',(phase4.config_hash(config),json.dumps(config)))
                for i in range(1,96):
                    phase4.save_prediction(i*3600,'openai','fixed',config,[.8,.1,.1],'same',.1,'QUIET')
                def attempt(target):
                    try:
                        phase4.save_prediction(target,'openai','fixed',config,[.8,.1,.1],'same',.1,'QUIET')
                        return 'saved'
                    except RuntimeError as error:
                        return str(error)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results=list(pool.map(attempt,(96*3600,97*3600)))
                self.assertEqual(results.count('saved'),1)
                self.assertEqual(sum('limit' in r for r in results),1)
                with patch.object(migration,'ROOT',root),patch.object(migration,'DB_PATH',db),patch.object(migration,'load_config',return_value=config):
                    migration.migrate(); migration.migrate()
                with sqlite3.connect(db) as c:
                    original=c.execute('SELECT * FROM volatility_predictions ORDER BY id').fetchall()
                for backup in root.glob('backups/*/btc.db'):
                    with sqlite3.connect(backup) as c:
                        self.assertEqual(original,c.execute('SELECT * FROM volatility_predictions ORDER BY id').fetchall())
                self.assertEqual(len(list(root.glob('backups/*/btc.db'))),2)

    def test_boundaries_migration_immutability_and_append_guard(self):
        import phase4_views as views
        config=json.loads(phase4.CONFIG_PATH.read_text())
        original_bytes=phase4.CONFIG_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'btc.db'
            with patch.object(phase4,'DB_PATH',db):
                phase4.init_schema()
                with sqlite3.connect(db) as c:
                    c.execute('CREATE TABLE predictions(candle_time INTEGER,predictor TEXT,phase TEXT,evaluated_at TEXT)')
                    c.executemany('INSERT INTO predictions VALUES (?,?,?,?)',
                                  [(i,m,'phase3','done') for i in range(48) for m in ('openai','jev')])
                    c.execute('INSERT INTO phase4_configs VALUES (?,?)',(phase4.config_hash(config),json.dumps(config)))
                for i in range(1,49):
                    for model in ('openai','jev'):
                        phase4.save_prediction(i*3600,model,'fixed',config,[.8,.1,.1],'unchanged',.1,'QUIET')
                with sqlite3.connect(db) as c:
                    before=c.execute('SELECT * FROM volatility_predictions ORDER BY id').fetchall()
                    frozen=c.execute('SELECT * FROM phase4_configs').fetchall()
                phase4.init_schema(); phase4.init_schema()
                with self.assertRaisesRegex(RuntimeError,'chronologically'):
                    phase4.save_prediction(0,'openai','fixed',config,[.8,.1,.1],'unchanged',.1,'QUIET')
                for i in range(49,97):
                    for model in ('openai','jev'):
                        phase4.save_prediction(i*3600,model,'fixed',config,[.8,.1,.1],'unchanged',.1,'QUIET')
                for model in ('openai','jev'):
                    with self.assertRaisesRegex(RuntimeError,'limit'):
                        phase4.save_prediction(97*3600,model,'fixed',config,[.8,.1,.1],'unchanged',.1,'QUIET')
                with sqlite3.connect(db) as c:
                    self.assertEqual(before,c.execute('SELECT * FROM volatility_predictions WHERE target_candle_time<=? ORDER BY id',(48*3600,)).fetchall())
                    self.assertEqual(frozen,c.execute('SELECT * FROM phase4_configs').fetchall())
                    groups=c.execute('SELECT predictor,cohort,count(*) FROM phase4_segments GROUP BY predictor,cohort ORDER BY predictor,cohort').fetchall()
                    self.assertEqual(groups,[(m,s,48) for m in ('jev','openai') for s in ('phase4a','phase4b')])
                    c.row_factory=sqlite3.Row
                    raw=[dict(r) for r in c.execute('SELECT * FROM volatility_predictions')]
                    derived=phase4.segment_rows(raw)
                    self.assertEqual({r['cohort'] for r in derived if r['prediction_number']==48},{'phase4a'})
                    self.assertEqual({r['cohort'] for r in derived if r['prediction_number'] in (49,96)},{'phase4b'})
                with sqlite3.connect(db) as c:
                    c.execute("UPDATE volatility_predictions SET actual_class=CASE WHEN target_candle_time<=? THEN 'QUIET' ELSE 'ACTIVE' END, evaluated_at='test'",(48*3600,))
                with patch.object(views,'DB_PATH',db):
                    for analysis in (False,True):
                        rendered=views.page(analysis=analysis)
                        for title in ('Phase 4a','Phase 4b','Combined'):
                            self.assertIn(title,rendered)
                        self.assertIn('phase4b</td><td>49',rendered)
                        self.assertIn('<td>48</td><td>48</td><td>100.0%</td><td>0.0600</td>',rendered)
                        self.assertIn('<td>48</td><td>48</td><td>0.0%</td><td>1.4600</td>',rendered)
                        self.assertIn('<td>96</td><td>96</td><td>50.0%</td><td>0.7600</td>',rendered)
                    import app as dashboard
                    client=dashboard.app.test_client()
                    for url in ('/','/analyze/?phase=phase4'):
                        response=client.get(url)
                        self.assertEqual(response.status_code,200)
                        self.assertIn(b'Phase 4b',response.data)
        self.assertEqual(original_bytes,phase4.CONFIG_PATH.read_bytes())
        self.assertEqual(config['prediction_limit'],48)
        with self.assertRaises(RuntimeError):phase4.prediction_limit(dict(config,prediction_limit=96))

if __name__=='__main__':unittest.main()
