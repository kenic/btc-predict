import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import phase4
import next_stages as stages
import next_stage_views as views
import app

class NextStagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/'btc.db'
        self.patches = [patch.object(m,'DB_PATH',self.db) for m in (phase4,stages,views)]
        for p in self.patches: p.start()
        phase4.init_schema()
        stages.migrate()
        with stages.database() as c:
            c.execute('INSERT INTO phase4_configs VALUES (?,?)', ('config',json.dumps({'quiet_upper':.2,'active_lower':.4})))
            for model in ('openai','jev'):
                for i in range(96):
                    if model=='jev' and i==20: continue
                    c.execute('''INSERT INTO volatility_predictions
                    (created_at,target_candle_time,predictor,model_version,config_id,
                    p_quiet,p_normal,p_active,context,previous_rv,persistence_class,actual_class)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', ('saved',i*3600,model,'version','config',.2,.3,.5,'EXACT saved context',.15,'QUIET','ACTIVE'))
        self.boundary=95*3600

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def call(self, source):
        self.assertEqual(source['context'],'EXACT saved context')
        self.assertEqual(source['model_version'],'version')
        return [.2,.3,.5],'version','{}'

    def test_boundary_missing_resume_and_transition(self):
        with stages.database() as c:
            before=[tuple(r) for r in c.execute('SELECT * FROM volatility_predictions')]
        self.assertEqual(stages.advance(self.boundary+3599),'4')
        self.assertEqual(stages.advance(self.boundary+3600),'4r')
        stages.run_repeats(self.call,budget=7)
        stages.run_repeats(self.call,budget=3)
        with stages.database() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM phase4r_runs').fetchone()[0],10)
        self.assertEqual(stages.advance(self.boundary+7200),'4r')
        stages.run_repeats(self.call,budget=2000)
        self.assertEqual(stages.advance(self.boundary+7200),'5')
        stages.run_repeats(lambda s: self.fail('duplicate API'),budget=2000)
        report=views.report()
        self.assertEqual(report['phase4r']['openai']['n'],96)
        self.assertEqual(report['phase4r']['jev']['n'],95)
        self.assertEqual(report['paired_n'],95)
        self.assertEqual(report['paired_evaluated_n'],95)
        self.assertEqual(report['phase4r']['openai']['completed_runs'],960)
        with stages.database() as c:
            self.assertEqual(before,[tuple(r) for r in c.execute('SELECT * FROM volatility_predictions')])
            first=c.execute("SELECT boundary FROM experiment_transitions WHERE stage='5'").fetchone()[0]
        stages.advance(self.boundary+99999)
        with stages.database() as c:
            self.assertEqual(first,c.execute("SELECT boundary FROM experiment_transitions WHERE stage='5'").fetchone()[0])

    def enable5(self):
        with stages.database() as c:
            stages.transition(c,'5',3600,{'limit':96})

    def test_regression_validation_stop_and_idempotence(self):
        self.enable5()
        calls=[]
        def call(snapshot):
            calls.append(snapshot)
            return 1000000.,'gpt','{}'
        for i in range(1,100):
            stages.run_regression(i*3600,clock=i*3600,call=call,builder=lambda t: ('same input',.2))
        self.assertEqual(len(calls),96)
        stages.run_regression(3600,clock=3600,call=lambda s:self.fail('duplicate'),builder=lambda t:self.fail('duplicate snapshot'))
        for value in (-1,float('nan'),float('inf'),'1',True):
            with self.assertRaises(ValueError): stages.validate_rv(value)
        self.assertEqual(stages.validate_rv(0),0)

    def test_target_pending_and_matching_baseline(self):
        self.enable5()
        stages.run_regression(3600,clock=3600,call=lambda s:(.3,'gpt','{}'),builder=lambda t:('context',.2))
        candles=[[t,0,0,0,100*math.exp(.001*i),0] for i,t in enumerate(range(3300,7200,300))]
        stages.evaluate_regression(fetch=lambda a,b:candles[:-1],clock=7200)
        self.assertEqual(views.report()['phase5']['model']['n'],0)
        stages.evaluate_regression(fetch=lambda a,b:candles,clock=7199)
        self.assertEqual(views.report()['phase5']['model']['n'],0)
        stages.evaluate_regression(fetch=lambda a,b:candles,clock=7200)
        r=views.report()['phase5']
        actual=100*math.sqrt(12*.001**2)
        self.assertAlmostEqual(r['predicted_vs_actual'][0]['actual_rv'],actual)
        self.assertAlmostEqual(r['model']['bias'],.3-actual)
        self.assertAlmostEqual(r['previous_hour_baseline']['bias'],.2-actual)
        self.assertEqual(r['model']['n'],r['previous_hour_baseline']['n'])

    def test_uncertain_call_is_not_retried(self):
        stages.advance(self.boundary+3600)
        with self.assertRaises(ValueError):
            stages.run_repeats(lambda s:(_ for _ in ()).throw(ValueError('network uncertainty')))
        with self.assertRaises(RuntimeError):
            stages.run_repeats(lambda s:self.fail('unsafe retry'))
        self.assertEqual(stages.advance(self.boundary+3600),'4r')

    def test_failed_output_is_not_clipped_or_counted(self):
        self.enable5()
        with self.assertRaises(ValueError):
            stages.run_regression(3600,clock=3600,call=lambda s:(-1,'gpt','{}'),builder=lambda t:('context',.2))
        stages.run_regression(7200,clock=7200,call=lambda s:(.3,'gpt','{}'),builder=lambda t:('context',.2))
        r=views.report()['phase5']
        self.assertEqual(r['predicted_n'],1)
        self.assertEqual(r['attempted_n'],2)

    def test_api_adapters_preserve_context_task_and_models(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from phase4_runner import instructions
        source={'id':1,'predictor':'openai','context':'EXACT saved context',
                'model_version':'original','config_json':json.dumps({'quiet_upper':.2,'active_lower':.4})}
        response=SimpleNamespace(output_text='{"p_quiet":0.2,"p_normal":0.3,"p_active":0.5}',model='returned',model_dump_json=lambda:'raw')
        client=MagicMock()
        client.responses.create.return_value=response
        factory=MagicMock(return_value=client)
        with patch.dict(sys.modules,{'openai':SimpleNamespace(OpenAI=factory)}):
            p,model,raw=stages.class_call(source)
        factory.assert_called_once_with(max_retries=0)
        kwargs=client.responses.create.call_args.kwargs
        self.assertEqual(kwargs['model'],'original')
        self.assertTrue(kwargs['input'].startswith(instructions(json.loads(source['config_json']))))
        self.assertTrue(kwargs['input'].endswith('MARKET DATA:\nEXACT saved context'))
        self.assertEqual((p,model,raw),([.2,.3,.5],'returned','raw'))
        source['predictor']='jev'
        client=MagicMock()
        client.system_one.return_value=SimpleNamespace(answers={'volatility':SimpleNamespace(probabilities={'QUIET':.2,'NORMAL':.3,'ACTIVE':.5})},model='jev-original',model_dump_json=lambda:'raw')
        factory=MagicMock(return_value=client)
        choice=MagicMock()
        retry=MagicMock()
        with patch.dict(sys.modules,{'typesafe_sdk':SimpleNamespace(TypeSafeClient=factory,Choice=choice,RetryPolicy=retry)}),patch.dict('os.environ',{'TYPESAFE_API_KEY':'fixture'}):
            stages.class_call(source)
        retry.assert_called_once_with(max_retries=0)
        self.assertEqual(client.system_one.call_args.kwargs['state'],'EXACT saved context')
        self.assertEqual(client.system_one.call_args.kwargs['model'],'original')
        self.assertEqual(choice.call_args.kwargs['instructions'],instructions(json.loads(source['config_json'])))

    def test_future_late_and_uncertain_regression_guards(self):
        self.enable5()
        never=lambda t:self.fail('invalid live cycle constructed a snapshot')
        stages.run_regression(0,clock=0,builder=never)
        stages.run_regression(3600,clock=4201,builder=never)
        stages.run_regression(7200,clock=7199,builder=never)
        with stages.database() as c:
            c.execute("INSERT INTO phase5_predictions(target_candle_time,started_at,status,context,previous_rv) VALUES (3600,'start','started','context',.2)")
        with self.assertRaises(RuntimeError):
            stages.run_regression(7200,clock=7200,builder=never)

    def test_approved_retry_audits_and_only_sends_once(self):
        stages.advance(self.boundary+3600)
        stages.run_repeats(self.call,budget=2000)
        with stages.database() as c:
            c.execute("UPDATE phase4r_runs SET predictor='jev',status='failed',error='Probabilities must sum to one',completed_at=NULL WHERE source_id=77 AND repeat_no=8")
            before=[tuple(r) for r in c.execute("SELECT * FROM phase4r_runs WHERE NOT(source_id=77 AND repeat_no=8)")]
        self.assertTrue(stages.authorize_approved_retry())
        self.assertFalse(stages.authorize_approved_retry())
        calls=[]
        stages.run_repeats(lambda source:(calls.append(source['id']) or [.2,.3,.5],'version','raw'),budget=2000)
        self.assertEqual(calls,[77])
        stages.run_repeats(lambda source:self.fail('duplicate retry'),budget=2000)
        with stages.database() as c:
            original=json.loads(c.execute('SELECT original_run_json FROM phase4r_retry_audit').fetchone()[0])
            self.assertEqual(original['error'],'Probabilities must sum to one')
            self.assertEqual(original['status'],'failed')
            self.assertEqual(before,[tuple(r) for r in c.execute("SELECT * FROM phase4r_runs WHERE NOT(source_id=77 AND repeat_no=8)")])
        self.assertEqual(len(views.report()['retry_exceptions']),1)

    def test_invalid_response_metadata_retained_and_no_second_retry(self):
        stages.advance(self.boundary+3600)
        stages.run_repeats(self.call,budget=2000)
        with stages.database() as c:
            c.execute("UPDATE phase4r_runs SET predictor='jev',status='failed',completed_at=NULL,error='Probabilities must sum to one' WHERE source_id=77 AND repeat_no=8")
        stages.authorize_approved_retry()
        def fail(source):
            raise stages.InvalidClassResponse(ValueError('Probabilities must sum to one'),'version','invalid response')
        with self.assertRaises(ValueError): stages.run_repeats(fail,budget=1)
        with stages.database() as c:
            row=c.execute("SELECT * FROM phase4r_runs WHERE status='failed' AND raw_json IS NOT NULL").fetchone()
            self.assertEqual(row['raw_json'],'invalid response')
            self.assertEqual(row['model_version'],'version')
        self.assertFalse(stages.authorize_approved_retry())
        with self.assertRaises(RuntimeError): stages.run_repeats(self.call)

    def test_routes(self):
        client=app.app.test_client()
        for route in ('/next-stages/','/analyze/?phase=phase4r','/analyze/?phase=phase5','/next-stages/data.json'):
            self.assertEqual(client.get(route).status_code,200)

if __name__=='__main__': unittest.main()
