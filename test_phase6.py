import json
import math
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import phase4
import next_stages
import phase6 as p6
import phase6_methods as methods
import phase6_views as views
import app


def snapshot(target=3600, missing=False):
    dt = datetime.fromtimestamp(target-3600,timezone.utc).strftime('%Y-%m-%d %H:%M')
    candles = '\n'.join(f'00:{i*5:02d} O=100.00 H=101.00 L=99.00 C={102 if i==11 else 100}.00 V=1.00' for i in range(12))
    return f'''Reference completed 1-hour candle:
{dt} UTC
Reference close:
100.00
=== 1-HOUR CONTEXT ===
Returns:
1h: 1.000%
SMA20: 102.00
SMA50: 100.00
Histogram: 1.00
=== 15-MINUTE CONTEXT ===
=== 5-MINUTE CONTEXT ===
5m: 0.100%
15m: 0.200%
Recent completed 5-minute candles:
{candles}
=== MARKET MICROSTRUCTURE ===
--- ORDER BOOK: AVERAGES ---
60m: {'N/A' if missing else 'samples=360, imb10=0.600, micro_offset=+0.1000'}
--- AGGRESSIVE TRADE FLOW ---
60m: {'N/A' if missing else 'trades=10, buy_ratio=0.600'}'''


class MethodsTests(unittest.TestCase):
    def test_gate_exact_definition(self):
        self.assertTrue(p6.gate('ACTIVE',['ACTIVE']*8+['NORMAL']*2)[0])
        self.assertFalse(p6.gate('NORMAL',['ACTIVE']*10)[0])
        self.assertFalse(p6.gate('ACTIVE',['ACTIVE']*7+['NORMAL']*3)[0])
        self.assertFalse(p6.gate('ACTIVE',['ACTIVE']*9)[0])
        self.assertFalse(p6.gate('ACTIVE',['ACTIVE']*9+[None])[0])

    def test_rules_missing_and_ensembles(self):
        f = methods.extract(snapshot())
        for method in methods.BASE_METHODS:
            if method in ('gpt','logistic'):
                continue
            result = methods.rule(method,f)
            self.assertEqual(result['direction'], 'DOWN' if method=='reversal_1h' else 'UP')
            self.assertIsNone(result['p_up'])
        f['breakout_close']=100
        self.assertEqual(methods.rule('breakout',f)['status'],'unavailable')
        missing = methods.extract(snapshot(missing=True))
        self.assertEqual(methods.rule('order_flow',missing)['status'],'unavailable')
        self.assertEqual(methods.rule('order_book',missing)['status'],'unavailable')
        results = {'gpt':methods.output(p_up=.8),'logistic':methods.output(p_up=.4),
                   'always_up':methods.output('UP'),'reversal_1h':methods.output('DOWN')}
        ensemble = methods.ensembles(results)
        self.assertEqual(ensemble['ensemble_vote']['status'],'unavailable')
        self.assertAlmostEqual(ensemble['ensemble_probability']['p_up'],.6)
        self.assertIsNone(methods.ensembles({'gpt':results['gpt']})['ensemble_probability']['p_up'])
        for invalid in (True,float('nan'),float('inf'),-.1,1.1,'0.5'):
            with self.assertRaises(ValueError): methods.output(p_up=invalid)

    def test_gpt_adapter_probability_validation_and_raw_audit(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        response = SimpleNamespace(model='returned-model',model_dump_json=lambda:'raw-response',output_text='{"p_up":0.6,"p_down":0.4}')
        client = MagicMock()
        client.responses.create.return_value = response
        factory = MagicMock(return_value=client)
        with patch.dict(sys.modules,{'openai':SimpleNamespace(OpenAI=factory)}):
            result = p6.direction_call(snapshot(),'requested-model')
            self.assertEqual(result['direction'],'UP')
            self.assertEqual(result['model_version'],'returned-model')
            self.assertEqual(result['raw_json'],'raw-response')
            self.assertTrue(client.responses.create.call_args.kwargs['input'].endswith(snapshot()))
            response.output_text = '{"p_up":0.6,"p_down":0.9}'
            invalid = p6.direction_call(snapshot(),'requested-model')
            self.assertEqual(invalid['status'],'unavailable')
            self.assertEqual(invalid['raw_json'],'raw-response')
        factory.assert_called_with(max_retries=0,timeout=120)

    def test_alignment(self):
        p6.aligned_features(snapshot(3600),3600)
        with self.assertRaises(ValueError): p6.aligned_features(snapshot(3600),7200)

    def test_logistic_strict_training_boundary(self):
        f = methods.extract(snapshot())
        cutoff = 1000*3600
        rows = [dict(id=str(i),target=i*3600,available_at=(i+2)*3600,
                     features=dict(f,return_1h=(-1 if i%2 else 1)),outcome='UP' if i%2 else 'DOWN') for i in range(120)]
        fit = methods.logistic(f,rows,cutoff)
        self.assertEqual(fit['status'],'complete')
        future = [dict(id='future',target=cutoff-3600,available_at=0,features=f,outcome='UP'),
                  dict(id='late_label',target=0,available_at=cutoff,features=f,outcome='UP'),
                  dict(id='future_target',target=cutoff,available_at=0,features=f,outcome='DOWN')]
        again = methods.logistic(f,rows+future,cutoff)
        self.assertEqual(fit,again)
        self.assertEqual(methods.logistic(f,rows[:99],cutoff)['status'],'unavailable')
        self.assertEqual(methods.logistic(f,[dict(r,outcome='UP') for r in rows],cutoff)['status'],'unavailable')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/'test.db'
        self.patches = [patch.object(m,'DB_PATH',self.db) for m in (p6,phase4,next_stages,app)]
        for p in self.patches: p.start()
        phase4.init_schema()
        next_stages.migrate()
        p6.migrate()
        self.cfg = p6.design({'quiet_upper':.2,'active_lower':.4},'test-model')
        self.cfg['implementation_hashes'] = p6.implementation_hashes()
        self.cfg['historical_frequency'] = {}
        with p6.database() as c:
            c.execute('INSERT INTO phase6_config VALUES(1,?,?,?,?,?)',
                      (json.dumps(self.cfg),phase4.config_hash(self.cfg),'start',3600,'test-sha'))
        self.calls = []

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def call(self,source):
        self.calls.append(source['context'])
        return [.05,.05,.9],'test-model','{"raw":"volatility"}'

    def run(self, target=3600, **kwargs):
        defaults = dict(clock=target,builder=lambda t:(snapshot(t),.2),class_call=self.call,
                        gpt_call=lambda context,model:dict(methods.output(p_up=.7),raw_json='raw',model_version=model))
        defaults.update(kwargs)
        p6.run(target,**defaults)

    # unittest.TestCase.run is reserved; expose the helper under a distinct name.
    run_hour = run
    del run

    def test_explicit_start_freezes_next_hour_once(self):
        with p6.database() as c:
            c.execute('DELETE FROM phase6_config')
            next_stages.transition(c,'5',3600,{'limit':96})
        with patch.object(phase4,'load_config',return_value={'quiet_upper':.2,'active_lower':.4}), patch.object(p6.subprocess,'check_output',side_effect=['abc123','']):
            p6.start(clock=3600)
        with p6.database() as c:
            first = dict(c.execute('SELECT * FROM phase6_config').fetchone())
            self.assertEqual(first['start_target'],7200)
            self.assertEqual(first['code_sha'],'abc123')
            self.assertEqual(first['config_hash'],phase4.config_hash(json.loads(first['config_json'])))
        p6.start(clock=99999)
        with p6.database() as c:
            self.assertEqual(first,dict(c.execute('SELECT * FROM phase6_config').fetchone()))

    def test_scheduler_isolates_phase5_and_phase6_failures(self):
        import sys
        import predict_gpt
        with patch.object(next_stages,'ROOT',Path(self.tmp.name)), patch.object(next_stages,'migrate'), patch.object(next_stages,'advance',return_value='5'), patch.object(predict_gpt,'get_prediction_cutoff',return_value=3600), patch.object(sys,'argv',['next_stages.py']):
            with patch.object(next_stages,'evaluate_regression',side_effect=RuntimeError('phase5 unavailable')), patch.object(p6,'cycle') as cycle:
                with self.assertRaisesRegex(RuntimeError,'phase5 unavailable'):
                    next_stages.main()
                cycle.assert_called_once()
            with patch.object(p6,'cycle',side_effect=RuntimeError('phase6 unavailable')), patch.object(next_stages,'evaluate_regression'), patch.object(next_stages,'run_regression') as regression:
                with self.assertRaisesRegex(RuntimeError,'phase6 unavailable'):
                    next_stages.main()
                regression.assert_called_once_with(3600)
            with open(Path(self.tmp.name)/'.next-stages.lock','a') as lock, patch.object(next_stages,'migrate') as migrate:
                p6.fcntl.flock(lock,p6.fcntl.LOCK_EX|p6.fcntl.LOCK_NB)
                next_stages.main()
                migrate.assert_not_called()

    def test_same_snapshot_idempotence_missing_and_additive(self):
        with p6.database() as c:
            old = [tuple(r) for r in c.execute('SELECT * FROM experiment_transitions')]
        self.run_hour()
        self.assertEqual(len(self.calls),11)
        self.assertEqual(set(self.calls),{snapshot()})
        self.run_hour(builder=lambda t:self.fail('Snapshot rebuilt'),class_call=lambda s:self.fail('Call replayed'))
        self.assertEqual(len(self.calls),11)
        with p6.database() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM phase6_predictions').fetchone()[0],len(methods.METHODS))
            logistic = c.execute("SELECT * FROM phase6_predictions WHERE method='logistic'").fetchone()
            self.assertEqual(logistic['status'],'unavailable')
            self.assertIn('100',logistic['reason'])
            self.assertEqual(old,[tuple(r) for r in c.execute('SELECT * FROM experiment_transitions')])
            self.assertEqual(c.execute('SELECT count(*) FROM phase5_predictions').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM phase4r_runs').fetchone()[0],0)

    def test_frozen_design_and_hashes_reject_drift(self):
        with p6.database() as c:
            changed = dict(self.cfg, deadline_seconds=900)
            c.execute('UPDATE phase6_config SET config_json=?', (json.dumps(changed),))
        with self.assertRaisesRegex(RuntimeError,'design differs'):
            self.run_hour()
        with p6.database() as c:
            changed = dict(self.cfg, implementation_hashes={})
            c.execute('UPDATE phase6_config SET config_json=?', (json.dumps(changed),))
        with self.assertRaisesRegex(RuntimeError,'hashes differ'):
            self.run_hour()
        self.assertEqual(self.calls,[])

    def test_rejected_does_not_run_direction(self):
        self.run_hour(class_call=lambda s:([.1,.8,.1],'test','raw'),gpt_call=lambda *a:self.fail('Rejected direction call'))
        with p6.database() as c:
            self.assertEqual(c.execute('SELECT gate FROM phase6_events').fetchone()[0],'rejected')
            self.assertEqual(c.execute('SELECT count(*) FROM phase6_calls').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM phase6_predictions').fetchone()[0],0)

    def test_invalid_repeat_missing_gpt_and_no_retry(self):
        def fail(source): raise ValueError('invalid probability')
        self.run_hour(class_call=fail,gpt_call=lambda *a:self.fail('Direction on invalid gate'))
        self.run_hour(class_call=lambda s:self.fail('Retry'))
        self.run_hour(7200,gpt_call=lambda *a:(_ for _ in ()).throw(ValueError('provider unavailable')))
        with p6.database() as c:
            self.assertEqual(c.execute("SELECT status FROM phase6_predictions WHERE target=7200 AND method='gpt'").fetchone()[0],'unavailable')
            self.assertEqual(c.execute("SELECT direction FROM phase6_predictions WHERE target=7200 AND method='always_up'").fetchone()[0],'UP')

    def test_evaluation_target_and_flat(self):
        self.run_hour()
        calls = []
        def fetch(t):
            calls.append(t)
            return [t,90,120,100,110,1]
        p6.evaluate(fetch,clock=7199)
        self.assertEqual(calls,[])
        p6.evaluate(fetch,clock=7200)
        p6.evaluate(lambda t:self.fail('Evaluate twice'),clock=7200)
        with p6.database() as c:
            p = c.execute("SELECT * FROM phase6_predictions WHERE method='gpt'").fetchone()
            self.assertAlmostEqual(p['signed_pnl'],10)
            self.assertEqual(p['correct'],1)
            self.assertAlmostEqual(p['brier'],.09)
        self.run_hour(7200)
        p6.evaluate(lambda t:[t,100,100,100,100,1],clock=10800)
        with p6.database() as c:
            p = c.execute("SELECT * FROM phase6_predictions WHERE target=7200 AND method='gpt'").fetchone()
            self.assertIsNone(p['correct'])
            self.assertIsNone(p['brier'])
            self.assertEqual(p['signed_pnl'],0)

    def test_start_boundary_deadline_stop_and_interrupted(self):
        self.run_hour(0)
        self.run_hour(3600,clock=4201)
        self.assertEqual(self.calls,[])
        with p6.database() as c:
            c.execute("INSERT INTO phase6_events(target,started_at,gate) VALUES (3600,'start','pending')")
            c.execute("INSERT INTO phase6_predictions(target,method,status,started_at,version) VALUES(3600,'gpt','started','start','v')")
        self.run_hour()
        with p6.database() as c:
            self.assertEqual(c.execute('SELECT gate FROM phase6_events').fetchone()[0],'rejected')
            self.assertEqual(c.execute('SELECT status FROM phase6_predictions').fetchone()[0],'unavailable')
            for i in range(2,98):
                c.execute("INSERT INTO phase6_events(target,started_at,gate) VALUES(?,'start','accepted')", (i*3600,))
        self.run_hour(98*3600)
        self.assertEqual(self.calls,[])

    def test_routes_readonly_and_metrics(self):
        self.run_hour()
        p6.evaluate(lambda t:[t,90,120,100,110,1],clock=7200)
        before = self.db.read_bytes()
        with app.app.test_client() as client:
            for route in ('/direction-active/','/phase6/','/analyze/?phase=phase6','/direction-active/data.json'):
                response = client.get(route)
                self.assertEqual(response.status_code,200)
            page = client.get('/analyze/?phase=phase6').get_data(as_text=True)
            self.assertIn('Paired comparisons',page)
            self.assertIn('≥8/10',page)
        self.assertEqual(before,self.db.read_bytes())
        data = views.report(self.db)
        self.assertEqual(data['accepted'],1)
        gpt = next(r for r in data['methods'] if r['method']=='gpt')
        self.assertEqual(gpt['n'],1)
        self.assertEqual(gpt['coverage_active'],1)
        self.assertEqual(gpt['coverage_accepted'],1)
        self.assertAlmostEqual(gpt['mean_signed_pnl'],10)

    def test_cache_reuses_only_identical_frozen_source(self):
        with p6.database() as c:
            cfg_json = json.dumps(self.cfg['gate']['volatility_config'])
            c.execute('INSERT INTO phase4_configs VALUES(?,?)',('test',cfg_json))
            c.execute('''INSERT INTO volatility_predictions(created_at,target_candle_time,predictor,model_version,config_id,
                p_quiet,p_normal,p_active,context,previous_rv,persistence_class) VALUES('start',3600,'openai','test-model','test',.05,.05,.9,?,.2,'NORMAL')''',(snapshot(),))
            original = dict(c.execute('SELECT p.*,cfg.config_json FROM volatility_predictions p JOIN phase4_configs cfg USING(config_id)').fetchone())
            c.execute('INSERT INTO phase4r_sources VALUES(?,?)',(original['id'],json.dumps(original)))
            for i in range(1,11):
                c.execute('''INSERT INTO phase4r_runs(source_id,repeat_no,predictor,target_candle_time,status,started_at,p_quiet,p_normal,p_active,argmax_class,model_version,raw_json)
                    VALUES(?,?,'openai',3600,'complete','start',.05,.05,.9,'ACTIVE','test-model','raw')''',(original['id'],i))
        self.run_hour(class_call=lambda s:self.fail('Cache not reused'))
        with p6.database() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM phase6_calls WHERE cache_ref IS NOT NULL').fetchone()[0],11)
            frequency = p6.historical_frequency(c)
            self.assertEqual(frequency['accepted'],1)
            self.assertEqual(frequency['gate_rate'],1)
            source = dict(context=snapshot()+'changed',target_candle_time=3600,model_version='test-model',config_json=cfg_json)
            self.assertEqual(p6.cached_calls(c,source),{})


if __name__ == '__main__':
    unittest.main()
