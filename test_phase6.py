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
            if method in ('gpt','logistic','random_50_50'):
                continue
            result = methods.rule(method,f)
            self.assertEqual(result['direction'], 'DOWN' if method in ('reversal_1h','always_down') else 'UP')
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

    def test_baselines_and_short_horizon_are_distinct(self):
        f = methods.extract(snapshot())
        self.assertEqual(methods.rule('always_up',f)['direction'],'UP')
        self.assertEqual(methods.rule('always_down',f)['direction'],'DOWN')
        for previous, momentum, reversal in ((1,'UP','DOWN'),(-1,'DOWN','UP'),(0,None,None)):
            features = dict(f,return_1h=previous)
            self.assertEqual(methods.rule('momentum_1h',features)['direction'],momentum)
            self.assertEqual(methods.rule('reversal_1h',features)['direction'],reversal)
        f['return_1h'] = -1
        self.assertEqual(methods.rule('momentum_1h',f)['direction'],'DOWN')
        self.assertEqual(methods.rule('momentum_short',f)['direction'],'UP')
        self.assertIn('momentum_1h',methods.BASELINES)
        self.assertIn('momentum_short',methods.DIRECTION_METHODS)
        self.assertFalse(set(methods.BASELINES)&set(methods.DIRECTION_METHODS))

    def test_random_baseline_reproducible_per_target_and_seed(self):
        result = methods.random_baseline(3600)
        self.assertEqual(result['direction'],'DOWN')
        self.assertEqual(result['audit']['draw_sha256'],'87a75ac79b21ad3160bb231a80b1ded0654e867f6a32094b2241d0b294712698')
        self.assertIsNone(result['p_up'])
        self.assertEqual(result['audit']['seed'],methods.RANDOM_SEED)
        targets = range(3600,97*3600,3600)
        forward = {t: methods.random_baseline(t) for t in targets}
        reverse = {t: methods.random_baseline(t) for t in reversed(targets)}
        self.assertEqual(forward,reverse)
        self.assertEqual({v['direction'] for v in forward.values()},{'UP','DOWN'})
        for invalid in (None,True,1,3600.0):
            with self.assertRaises(ValueError): methods.random_baseline(invalid)

    def test_ensembles_exclude_every_baseline_and_abstain_on_ties(self):
        direction_results = {'gpt':methods.output(p_up=.8),'logistic':methods.output(p_up=.2),
                             'breakout':methods.output('UP'),'momentum_short':methods.output('DOWN')}
        # Even hypothetical baseline probabilities cannot enter either ensemble.
        baselines = {m:methods.output(p_up=1) for m in methods.BASELINES}
        expected = methods.ensembles(direction_results)
        self.assertEqual(expected,methods.ensembles({**direction_results,**baselines}))
        self.assertEqual(expected['ensemble_vote']['status'],'unavailable')
        self.assertIsNone(expected['ensemble_vote']['direction'])
        self.assertEqual(expected['ensemble_probability']['p_up'],.5)
        self.assertEqual(expected['ensemble_probability']['status'],'unavailable')
        self.assertIsNone(expected['ensemble_probability']['direction'])
        self.assertEqual(expected['ensemble_probability']['audit']['eligible'],['gpt','logistic'])
        self.assertEqual(expected['ensemble_vote']['audit']['eligible'],sorted(direction_results))
        only_baselines = methods.ensembles(baselines)
        self.assertTrue(all(v['status']=='unavailable' and v['audit']['eligible']==[] for v in only_baselines.values()))
        direction_results['momentum_short'] = methods.output('UP')
        self.assertEqual(methods.ensembles(direction_results)['ensemble_vote']['direction'],'UP')
        direction_results['gpt'] = methods.output(p_up=.9)
        self.assertEqual(methods.ensembles(direction_results)['ensemble_probability']['direction'],'UP')
        direction_results['gpt'] = methods.output(p_up=.1)
        self.assertEqual(methods.ensembles(direction_results)['ensemble_probability']['direction'],'DOWN')
        # Existing minimum membership rules remain fixed.
        self.assertEqual(methods.ensembles({'gpt':direction_results['gpt'],'breakout':direction_results['breakout']})['ensemble_vote']['status'],'unavailable')
        self.assertEqual(methods.ensembles({'gpt':direction_results['gpt']})['ensemble_probability']['status'],'unavailable')
        # Individual GPT/logistic tie behavior is deliberately unchanged.
        self.assertEqual(methods.output(p_up=.5)['direction'],'UP')

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
        self.cfg['historical_frequency'] = {'interpretation': 'Historical repeat audit only; missing ensembles excluded; future frequency unknown'}
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

    def test_random_saved_once_and_direction_membership_audited(self):
        self.run_hour()
        expected = methods.random_baseline(3600)
        with p6.database() as c:
            random = dict(c.execute("SELECT * FROM phase6_predictions WHERE method='random_50_50'").fetchone())
            self.assertEqual(random['direction'],expected['direction'])
            self.assertEqual(json.loads(random['audit_json']),expected['audit'])
            self.assertEqual(c.execute("SELECT direction FROM phase6_predictions WHERE method='always_down'").fetchone()[0],'DOWN')
            for ensemble in methods.DIRECTION_ENSEMBLES:
                audit = json.loads(c.execute('SELECT audit_json FROM phase6_predictions WHERE method=?',(ensemble,)).fetchone()[0])
                self.assertFalse(set(audit['eligible'])&set(methods.BASELINES))
        self.run_hour(class_call=lambda s:self.fail('No replay'))
        with p6.database() as c:
            self.assertEqual(random,dict(c.execute("SELECT * FROM phase6_predictions WHERE method='random_50_50'").fetchone()))
        report = views.report(self.db)
        self.assertEqual(len(report['ensemble_membership']),2)
        self.assertTrue(all(not set(r['eligible'])&set(methods.BASELINES) for r in report['ensemble_membership']))

    def test_no_start_marker_means_no_calls_or_implicit_start(self):
        with p6.database() as c:
            c.execute('DELETE FROM phase6_config')
        before = self.db.read_bytes()
        self.run_hour(builder=lambda t:self.fail('Snapshot without start'),
                      class_call=lambda s:self.fail('Volatility call without start'),
                      gpt_call=lambda *args:self.fail('Direction call without start'))
        with app.app.test_client() as client:
            rendered = client.get('/direction-active/').get_data(as_text=True)
            self.assertIn('Not started',rendered)
            for label in methods.METHODS:
                self.assertIn(views.METHOD_LABELS[label],rendered)
        self.assertEqual(before,self.db.read_bytes())
        with p6.database() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM phase6_config').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM phase6_events').fetchone()[0],0)

    def test_existing_start_state_is_never_rewritten(self):
        # Representative existing v1 preregistration; never rewrite its marker.
        cfg = json.loads(json.dumps(self.cfg))
        cfg['version'] = 'phase6-v1'
        cfg['methods'] = {m:'phase6-v1' for m in methods.METHODS if m not in ('always_down','random_50_50')}
        cfg['ensemble']['members'] = ['gpt','momentum_1h','momentum_short','reversal_1h',
                                      'always_up','breakout','trend','order_flow','order_book','logistic']
        cfg['ensemble']['probability_tie'] = 'UP'
        cfg.pop('categories')
        cfg.pop('random_baseline')
        cfg['implementation_hashes'] = {}
        with p6.database() as c:
            c.execute('UPDATE phase6_config SET config_json=?,config_hash=?',(json.dumps(cfg),phase4.config_hash(cfg)))
            before = dict(c.execute('SELECT * FROM phase6_config').fetchone())
        p6.start(clock=99999)
        with self.assertRaisesRegex(RuntimeError,'design differs'):
            self.run_hour()
        with p6.database() as c:
            self.assertEqual(before,dict(c.execute('SELECT * FROM phase6_config').fetchone()))
        self.assertEqual(self.calls,[])

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
            self.assertEqual(client.get('/direction-active/data.json').get_json()['config'],self.cfg)
            from html import unescape
            for route in ('/direction-active/', '/analyze/?phase=phase6'):
                rendered = unescape(client.get(route).get_data(as_text=True))
                for label in ('Single-shot volatility = ACTIVE', 'Repeat modal class = ACTIVE',
                              'Repeat agreement >= 8/10', '10-shot repeat agreement',
                              'ACTIVE repeat agreement >= 8/10', 'Direction ensemble',
                              'Majority-vote Direction Ensemble', 'Probability-average Direction Ensemble',
                              '<th>ACTIVE repeat agreement</th>'):
                    self.assertIn(label,rendered)
                self.assertNotIn('including ensembles',rendered)
                self.assertNotIn('Probability ensemble',rendered)
                self.assertNotIn('missing ensembles excluded',rendered)
                for category in ('Volatility Gate','Baselines','Direction Methods','Direction Ensembles'):
                    self.assertIn('<h2>'+category+'</h2>',rendered)
                positions = [rendered.index('<h2>'+category+'</h2>') for category in ('Volatility Gate','Baselines','Direction Methods','Direction Ensembles')]
                self.assertEqual(positions,sorted(positions))
                for label in ('Always UP','Always DOWN','Random 50/50','1h Momentum Baseline',
                              '1h Reversal Baseline','Short-horizon Momentum','Saved Direction ensemble membership',
                              'FLAT','No epsilon dead-zone','never counted as incorrect'):
                    self.assertIn(label,rendered)
            self.assertIn('expected accuracy = 50%',page)
            self.assertIn('binary Brier = 0.25',page)
        self.assertEqual(before,self.db.read_bytes())
        data = views.report(self.db)
        self.assertEqual(data['accepted'],1)
        gpt = next(r for r in data['methods'] if r['method']=='gpt')
        self.assertEqual(gpt['n'],1)
        self.assertEqual(gpt['coverage_active'],1)
        self.assertEqual(gpt['coverage_accepted'],1)
        self.assertAlmostEqual(gpt['mean_signed_pnl'],10)

    def test_real_local_archives_independent_of_gate_and_network(self):
        from volatility import realized_volatility, classify
        for target, probabilities in ((3600,[.9,.05,.05]),(7200,[.05,.9,.05]),
                                      (10800,[.05,.05,.9]),(14400,[.05,.05,.9])):
            votes = iter([probabilities]+([[.9,.05,.05]]*10 if target==10800 else [probabilities]*10))
            self.run_hour(target,class_call=lambda source:(next(votes),'test-model','raw'))
        candles = [[t,90,120,100,100*1.0001**i,1]
                   for i,t in enumerate(range(3300,18000,300))]
        (self.db.parent/'direction_history_5m.json').write_text(json.dumps(candles))
        p6.evaluate(lambda t:[t,90,120,100,next(x[4] for x in candles if x[0]==t+3300),1],clock=18000)
        before = self.db.read_bytes()
        hashes = p6.implementation_hashes()
        with patch.object(views,'ROOT',self.db.parent), patch.object(views.time,'time',return_value=18000), \
             patch('requests.get',side_effect=AssertionError('No external fetch')), \
             patch('urllib.request.urlopen',side_effect=AssertionError('No external fetch')), \
             patch.object(p6,'run',side_effect=AssertionError('No prediction rerun')):
            report = views.report(self.db)
            self.assertEqual([r['gate'] for r in report['recent']],['accepted','rejected','rejected','rejected'])
            for row in report['recent']:
                expected = realized_volatility(candles,row['target'])
                self.assertEqual(row['actual_volatility'],expected)
                self.assertEqual(row['actual_regime'],classify(expected,self.cfg['gate']['volatility_config']))
            # Conflict outside a target window cannot poison that window.
            (self.db.parent/'phase4_history_5m.json').write_text(json.dumps([[3300,90,120,100,999,1]]))
            rows = views.report(self.db)['recent']
            self.assertEqual(rows[-1]['actual_volatility_status'],'unavailable')
            self.assertTrue(all(r['actual_volatility_status']=='complete' for r in rows[:-1]))
            # A missing close stays missing; no zero fill/interpolation.
            (self.db.parent/'direction_history_5m.json').write_text(json.dumps([x for x in candles if x[0]!=15000]))
            self.assertEqual(views.report(self.db)['recent'][0]['actual_volatility_status'],'unavailable')
        self.assertEqual(before,self.db.read_bytes())
        self.assertEqual(hashes,p6.implementation_hashes())

    def test_dedicated_candle_db_all_gates_and_routes_are_read_only(self):
        import candle_store
        import collect_candles
        from volatility import realized_volatility, classify
        for target, p in ((3600,[.9,.05,.05]),(7200,[.05,.9,.05]),
                          (10800,[.05,.05,.9]),(14400,[.05,.05,.9])):
            votes = iter([p]+([[.9,.05,.05]]*10 if target==10800 else [p]*10))
            self.run_hour(target,class_call=lambda s:(next(votes),'test-model','raw'))
        candles = [[t,90,120,100,100*1.0001**i,1]
                   for i,t in enumerate(range(3300,18000,300))]
        archive = self.db.parent/'candles.db'
        candle_store.initialize(archive,3300)
        candle_store.save(archive,candles,18060)
        p6.evaluate(lambda t:[t,90,120,100,next(x[4] for x in candles if x[0]==t+3300),1],clock=18000)
        before = self.db.read_bytes()
        calls = len(self.calls)
        hashes = p6.implementation_hashes()
        with patch.object(views.time,'time',return_value=18000), \
             patch('requests.get',side_effect=AssertionError('No external web fetch')), \
             patch('urllib.request.urlopen',side_effect=AssertionError('No external web fetch')), \
             patch.object(p6,'run',side_effect=AssertionError('No predictor rerun')):
            with app.app.test_client() as client:
                for url in ('/direction-active/','/analyze/?phase=phase6','/direction-active/data.json'):
                    self.assertEqual(client.get(url).status_code,200)
                data = client.get('/direction-active/data.json').get_json()
                self.assertEqual([r['gate'] for r in data['recent']],['accepted','rejected','rejected','rejected'])
                for row in data['recent']:
                    rv = realized_volatility(candles,row['target'])
                    self.assertEqual(row['actual_volatility'],rv)
                    self.assertEqual(row['actual_regime'],classify(rv,self.cfg['gate']['volatility_config']))
        with self.assertRaises(RuntimeError):
            collect_candles.collect(archive,lambda a,b:(_ for _ in ()).throw(RuntimeError('provider down')),18060)
        self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual(len(self.calls),calls)
        self.assertEqual(p6.implementation_hashes(),hashes)

    def test_return_direction_completion_status(self):
        self.run_hour()
        with patch.object(views.time,'time',return_value=7199):
            row = views.report(self.db)['recent'][0]
            for key in ('actual_return','actual_volatility','actual_regime','actual_direction'):
                self.assertEqual(views.format_value(key,row[key],row),'pending')
        with patch.object(views.time,'time',return_value=7200):
            row = views.report(self.db)['recent'][0]
            for key in ('actual_return','actual_volatility','actual_regime','actual_direction'):
                self.assertEqual(views.format_value(key,row[key],row),'unavailable')


    def test_reverse_gpt_historical_readonly_analysis(self):
        hashes = p6.implementation_hashes()
        for target, probability, close in ((3600,.7,110),(7200,.2,90),(10800,.7,90),(14400,.2,110),(18000,.5,100)):
            self.run_hour(target,gpt_call=lambda context,model,p=probability:
                          dict(methods.output(p_up=p),raw_json='raw',model_version=model))
            p6.evaluate(lambda t,c=close:[t,100,120,80,c,1],clock=target+3600)
        self.run_hour(21600,gpt_call=lambda *a:(_ for _ in ()).throw(ValueError('unavailable')))
        self.run_hour(25200)  # Pending evaluation remains unscored.
        with p6.database() as c:
            stored = [dict(r) for r in c.execute("SELECT p.*,e.actual_return,e.actual_direction FROM phase6_predictions p JOIN phase6_events e USING(target) WHERE method='gpt'")]
            count = c.execute('SELECT count(*) FROM phase6_predictions').fetchone()[0]
        before = self.db.read_bytes()
        for original in stored:
            reverse = views.reverse_gpt(original)
            if original['status'] != 'complete':
                self.assertEqual(reverse['status'],'unavailable')
                self.assertIsNone(reverse['signed_pnl'])
                continue
            self.assertEqual(reverse['direction'],{'UP':'DOWN','DOWN':'UP'}[original['direction']])
            self.assertEqual((reverse['p_up'],reverse['p_down']),(original['p_down'],original['p_up']))
            if original['signed_pnl'] is not None:
                self.assertEqual(reverse['signed_pnl'],-original['signed_pnl'])
            else:
                self.assertIsNone(reverse['signed_pnl'])
            if original['actual_direction'] in ('UP','DOWN'):
                self.assertEqual(reverse['correct'],1-original['correct'])
                self.assertEqual(reverse['brier'],(original['p_down']-int(original['actual_direction']=='UP'))**2)
            else:
                self.assertIsNone(reverse['correct'])
                self.assertIsNone(reverse['brier'])
        for direction in (None,'FLAT','invalid'):
            invalid = dict(stored[0],direction=direction)
            self.assertEqual(views.reverse_gpt(invalid)['status'],'unavailable')
        with patch.object(p6,'direction_call',side_effect=AssertionError('New model call')), patch.object(p6,'run',side_effect=AssertionError('Live prediction')):
            data = views.report(self.db)
            with app.app.test_client() as client:
                for route in ('/direction-active/','/analyze/?phase=phase6'):
                    response = client.get(route)
                    self.assertEqual(response.status_code,200)
                    html = response.get_data(as_text=True)
                    self.assertIn('Reverse GPT',html)
                    self.assertIn('analysis-only anti-signal',html)
                    self.assertIn('no additional model call',html)
        metrics = {r['method']:r for r in data['methods']}
        for key in ('n','available','pnl_n','brier_n','coverage_all','coverage_active','coverage_accepted','mean_absolute_return','median_absolute_return'):
            self.assertEqual(metrics['gpt'][key],metrics['reverse_gpt'][key])
        self.assertEqual(metrics['reverse_gpt']['mean_signed_pnl'],-metrics['gpt']['mean_signed_pnl'])
        self.assertEqual(metrics['reverse_gpt']['correct'],metrics['gpt']['n']-metrics['gpt']['correct'])
        self.assertTrue(any(r['left']=='gpt' and r['right']=='reverse_gpt' for r in data['paired']))
        for membership in data['ensemble_membership']:
            self.assertNotIn('reverse_gpt',membership['eligible'])
        self.assertNotIn('reverse_gpt',methods.METHODS)
        self.assertNotIn('reverse_gpt',methods.DIRECTION_METHODS)
        outputs = {m:methods.output(p_up=.7) for m in methods.DIRECTION_METHODS}
        expected = methods.ensembles(outputs)
        outputs['reverse_gpt'] = methods.output(p_up=.3)
        self.assertEqual(methods.ensembles(outputs),expected)
        self.assertEqual(hashes,p6.implementation_hashes())
        self.assertEqual(before,self.db.read_bytes())
        with p6.database() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM phase6_predictions').fetchone()[0],count)
            self.assertEqual(c.execute("SELECT count(*) FROM phase6_predictions WHERE method='reverse_gpt'").fetchone()[0],0)

    def test_actual_volatility_reuses_phase4_calculation_and_frozen_regime(self):
        from volatility import realized_volatility, classify
        self.run_hour()
        target = 3600
        candles = {t:[t,90,120,100,100*1.001**i,1]
                   for i,t in enumerate(range(target-300,target+3600,300))}
        last = candles[target+3300][4]
        p6.evaluate(lambda t:[t,90,120,100,last,1],clock=7200)
        expected = realized_volatility(list(candles.values()),target)
        before = self.db.read_bytes()
        with patch.object(views,'load_candles',return_value=candles), patch('direction_volatility.realized_volatility',wraps=realized_volatility) as calculation:
            report = views.report(self.db)
            calculation.assert_called_once()
            row = report['recent'][0]
            self.assertEqual(row['actual_volatility'],expected)
            self.assertEqual(row['actual_regime'],classify(expected,self.cfg['gate']['volatility_config']))
            self.assertEqual(row['actual_volatility_status'],'complete')
            page = views.page(self.db)
            self.assertIn(f'{expected:.4f}%',page)
            self.assertIn(f'{100*(last/100-1):+.4f}%',page)
        self.assertEqual(before,self.db.read_bytes())

    def test_saved_phase5_actual_rv_reused_with_exact_phase4_thresholds(self):
        self.run_hour()
        for value,regime in ((.199999999,'QUIET'),(.2,'NORMAL'),(.4,'ACTIVE')):
            with p6.database() as c:
                c.execute("INSERT OR REPLACE INTO phase5_predictions(target_candle_time,started_at,status,context,previous_rv,actual_rv,evaluated_at) VALUES(3600,'start','complete','saved',.1,?,'evaluated')",(value,))
            before = self.db.read_bytes()
            with patch.object(views,'load_candles',side_effect=AssertionError('No cache or data refetch needed')):
                row = views.report(self.db)['recent'][0]
                self.assertEqual(row['actual_volatility'],value)
                self.assertEqual(row['actual_regime'],regime)
            self.assertEqual(before,self.db.read_bytes())

    def test_unfinished_target_pending_and_missing_candle_not_interpolated(self):
        self.run_hour()
        candles = {t:[t,90,120,100,100*1.001**i,1]
                   for i,t in enumerate(range(3300,7200,300))}
        with patch.object(views.time,'time',return_value=7199), patch.object(views,'load_candles',side_effect=AssertionError('Do not calculate an open hour')):
            row = views.report(self.db)['recent'][0]
            self.assertIsNone(row['actual_volatility'])
            self.assertIsNone(row['actual_regime'])
            self.assertEqual(row['actual_volatility_status'],'pending')
            self.assertEqual(views.format_value('actual_volatility',None,row),'pending')
            self.assertEqual(views.format_value('actual_regime',None,row),'pending')
        del candles[5100]
        with patch.object(views,'load_candles',return_value=candles), patch('prepare_phase4.fetch_candles',side_effect=AssertionError('No network fetch from dashboard')):
            row = views.report(self.db)['recent'][0]
            self.assertIsNone(row['actual_volatility'])
            self.assertIsNone(row['actual_regime'])
            self.assertEqual(row['actual_volatility_status'],'unavailable')
            self.assertEqual(views.format_value('actual_volatility',None,row),'unavailable')

    def test_conflicting_cached_target_close_is_unavailable(self):
        self.run_hour()
        p6.evaluate(lambda t:[t,90,120,100,110,1],clock=7200)
        candles = {t:[t,90,120,100,100,1] for t in range(3300,7200,300)}
        with patch.object(views,'load_candles',return_value=candles):
            row = views.report(self.db)['recent'][0]
            self.assertEqual(row['actual_volatility_status'],'unavailable')
            self.assertIsNone(row['actual_volatility'])
            self.assertAlmostEqual(row['actual_return'],10)

    def test_recent_hours_format_column_order_and_raw_precision(self):
        self.run_hour()
        p6.evaluate(lambda t:[t,90,120,100,99.82948820898182,1],clock=7200)
        with p6.database() as c:
            # Bind floats like the production writers do; a decimal literal in SQL
            # text may parse to a neighbouring double and break exact comparison.
            c.execute("INSERT INTO phase5_predictions(target_candle_time,started_at,status,context,previous_rv,actual_rv,evaluated_at) VALUES(?,?,?,?,?,?,?)",
                      (3600,'start','complete','saved',.1,.4127123456789,'evaluated'))
        before = self.db.read_bytes()
        with app.app.test_client() as client:
            for route in ('/direction-active/','/analyze/?phase=phase6'):
                page = client.get(route).get_data(as_text=True)
                recent = page.split('<h2>Recent hours · accepted and rejected</h2>')[1].split('</section>')[0]
                self.assertIn('-0.1705%',recent)
                self.assertIn('0.4127%',recent)
                self.assertNotIn('None',recent)
                labels = ('Target','Gate','Single-shot volatility','ACTIVE repeat agreement','Reason',
                          'Actual return','Actual volatility','Actual regime','Actual direction',
                          'Repeat modal class','Repeat agreement')
                positions = [recent.index('<th>'+label+'</th>') for label in labels]
                self.assertEqual(positions,sorted(positions))
            raw = client.get('/direction-active/data.json').get_json()['recent'][0]
            self.assertEqual(raw['actual_volatility'],.4127123456789)
            self.assertNotEqual(raw['actual_return'],round(raw['actual_return'],4))
        self.assertEqual(before,self.db.read_bytes())
        self.assertEqual(views.format_value('actual_return',.1286123),'+0.1286%')
        self.assertEqual(views.format_value('actual_return',None),'pending')
        self.assertEqual(views.format_value('p_up',.612345),'0.612')
        self.assertEqual(views.format_value('accuracy',.612345),'61.2%')
        self.assertEqual(views.format_value('brier',.123456),'0.1235')

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
