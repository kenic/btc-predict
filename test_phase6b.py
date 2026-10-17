"""Offline temporary-DB boundary, immutability and frozen runtime parity checks."""
import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import phase4
import next_stages
import phase6
import phase6b as b
import phase6b_runtime as runtime
import phase6_methods as methods
import phase6_views
import app
from test_phase6 import snapshot


class Phase6bTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/'test.db'
        self.patches = [patch.object(m,'DB_PATH',self.db) for m in (phase4,next_stages,phase6,b,app)]
        for p in self.patches: p.start()
        phase4.init_schema(); next_stages.migrate(); phase6.migrate()
        cfg = phase6.design({'quiet_upper':.2,'active_lower':.4},'test-model')
        cfg['implementation_hashes'] = phase6.implementation_hashes()
        with b.database() as c:
            c.execute('INSERT INTO phase6_config VALUES(1,?,?,?,?,?)',
                (json.dumps(cfg),phase4.config_hash(cfg),'start',3600,'original'))
        self.calls = []
        self.runtime_run = runtime.run

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def seed(self,n=96):
        with b.database() as c:
            c.executemany("INSERT INTO phase6_events(target,started_at,gate,actual_direction,evaluated_at) VALUES(?,'start','accepted','UP','1970-01-01T00:00:01+00:00')",[(i*3600,) for i in range(1,n+1)])

    def start(self):
        with patch.object(b.subprocess,'check_output',side_effect=['new-sha','']):
            return b.start(clock=96*3600)

    def run_hour(self,target,original=False,**kwargs):
        def call(source):
            self.calls.append(source)
            return [.05,.05,.9],'test-model','raw'
        defaults=dict(clock=target,builder=lambda t:(snapshot(t),.2),class_call=call,
            gpt_call=lambda context,model:dict(methods.output(p_up=.7),raw_json='raw',model_version=model))
        defaults.update(kwargs)
        (phase6.run if original else self.runtime_run)(target,**defaults)

    def test_95_96_97_closure_and_primary_immutability(self):
        self.seed(95)
        with self.assertRaisesRegex(RuntimeError,'exactly 96'): self.start()
        self.run_hour(96*3600,original=True)
        self.assertEqual(len(self.calls),11)
        self.run_hour(97*3600,original=True)
        self.assertEqual(len(self.calls),11)  # Original really stops at96.
        first=self.start()  # Pending96th actual must not delay97th target.
        self.assertEqual(first['runtime_config']['start_target'],97*3600)
        self.assertFalse(b.status()['phase6_results_sealed'])
        self.assertEqual(first,b.start(clock=999999))
        self.run_hour(96*3600)  # No old-target backfill.
        self.assertEqual(len(self.calls),11)
        self.run_hour(97*3600)
        self.assertEqual(len(self.calls),22)
        # Original evaluator completes96 while 6b is already running.
        phase6.evaluate(lambda t:[t,100,110,90,101,1],clock=97*3600)
        with b.database() as c:
            b.validate(c)
            b.seal_primary(c)
            before=phase6_snapshot(c)
        runtime.evaluate(lambda t:[t,100,110,90,102,1],clock=98*3600)
        with b.database() as c:
            self.assertEqual(before,phase6_snapshot(c))
            row=c.execute("SELECT * FROM phase6b_predictions WHERE method='always_up'").fetchone()
            self.assertAlmostEqual(row['signed_pnl'],2)
            self.assertEqual(row['correct'],1)
        self.assertTrue(b.status()['phase6_results_sealed'])
        self.assertEqual(b.status()['cumulative_accepted'],97)

    def test_199_200_stop_and_final_evaluation(self):
        self.seed(); self.start()
        with b.database() as c:
            c.executemany("INSERT INTO phase6b_events(target,started_at,gate) VALUES(?,'start','accepted')",[(i*3600,) for i in range(97,200)])
        self.run_hour(200*3600)
        self.assertEqual(len(self.calls),11)
        self.run_hour(201*3600)
        self.assertEqual(len(self.calls),11)
        runtime.evaluate(lambda t:[t,100,110,90,99,1],clock=201*3600)
        self.assertEqual(b.status()['cumulative_accepted'],200)
        with b.database() as c:
            self.assertIsNotNone(c.execute('SELECT evaluated_at FROM phase6b_events WHERE target=?',(200*3600,)).fetchone()[0])

    def test_unstarted_and_hash_drift_fail_before_calls(self):
        self.seed()
        with self.assertRaisesRegex(RuntimeError,'not started'): self.run_hour(97*3600)
        self.start()
        with patch.object(b,'hashes',return_value={}):
            with self.assertRaisesRegex(RuntimeError,'implementation changed'): self.run_hour(97*3600)
        with b.database() as c: c.execute('UPDATE phase6_events SET reason=? WHERE target=3600',('changed',))
        with self.assertRaisesRegex(RuntimeError,'closure'): self.run_hour(97*3600)
        self.assertEqual(self.calls,[])

    def test_original_config_drift_and_unfinished_predictions_block_start(self):
        self.seed()
        with b.database() as c:
            c.execute("INSERT INTO phase6_predictions(target,method,status,started_at,version) VALUES(3600,'gpt','started','start','v')")
        with self.assertRaisesRegex(RuntimeError,'Unfinished'): self.start()
        with b.database() as c:
            c.execute('DELETE FROM phase6_predictions')
            c.execute("UPDATE phase6_config SET config_hash='drift'")
        with self.assertRaisesRegex(RuntimeError,'hash mismatch'): self.start()

    def test_training_continues_with_both_cohorts_and_strict_cutoff(self):
        self.seed()
        with b.database() as c:
            context=snapshot(3600)
            c.execute('UPDATE phase6_events SET features_json=?,snapshot_hash=? WHERE target=3600',
                      (json.dumps(phase6.aligned_features(context,3600)),phase6.digest(context)))
        self.start(); self.run_hour(97*3600)
        runtime.evaluate(lambda t:[t,100,110,90,102,1],clock=98*3600)
        with b.database() as c: rows=runtime.training_rows(c)
        self.assertTrue(any(r['id']=='phase6:3600' for r in rows))
        self.assertTrue(any(r['id']=='phase6b:'+str(97*3600) for r in rows))
        # Existing logistic implementation rejects same-hour and late outcomes.
        f=phase6.aligned_features(snapshot(99*3600),99*3600)
        old=[dict(id=str(i),target=i*3600,available_at=i*3600+3601,
                  outcome='UP' if i%2 else 'DOWN',features=f) for i in range(120)]
        cutoff=200*3600
        future=dict(old[0],id='future',target=cutoff-3600,available_at=cutoff)
        self.assertEqual(methods.logistic(f,old,cutoff),methods.logistic(f,old+[future],cutoff))

    def test_status_readonly_and_no_implicit_start(self):
        self.seed(65)
        before=self.db.read_bytes()
        self.assertFalse(b.status()['phase6b_started'])
        self.assertEqual(before,self.db.read_bytes())

    def test_automatic_cycle_waits_without_writes_before96(self):
        self.seed(65)
        before=self.db.read_bytes()
        with patch('predict_gpt.get_prediction_cutoff',return_value=66*3600), patch.object(runtime,'run') as run, patch.object(b,'start') as start:
            b.cycle()
            run.assert_not_called(); start.assert_not_called()
        self.assertEqual(before,self.db.read_bytes())
        with b.database() as c:
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'phase6b_%'").fetchone())

    def test_auto_handoff_no_evaluation_gap_no_duplicate_and_final_seal(self):
        self.seed(95); self.run_hour(96*3600,original=True)
        self.calls.clear()
        with patch('predict_gpt.get_prediction_cutoff',return_value=97*3600), patch.object(b.subprocess,'check_output',side_effect=['auto-sha','']), patch.object(runtime,'run',side_effect=self.run_hour), patch.object(runtime,'evaluate'):
            b.cycle()
            b.cycle()  # Same hour cannot repeat calls or reset start.
        self.assertEqual(len(self.calls),11)
        self.assertEqual(b.status()['cumulative_accepted'],97)
        self.assertFalse(b.status()['phase6_results_sealed'])
        phase6.evaluate(lambda t:[t,100,110,90,101,1],clock=97*3600)
        with patch('predict_gpt.get_prediction_cutoff',return_value=98*3600), patch.object(runtime,'run'), patch.object(runtime,'evaluate'):
            b.cycle()
        self.assertTrue(b.status()['phase6_results_sealed'])
        with b.database() as c:
            c.execute('UPDATE phase6_predictions SET signed_pnl=999 WHERE target=? AND method=?',(96*3600,'gpt'))
        with self.assertRaisesRegex(RuntimeError,'Final Phase6'): b.status()

    def test_already_evaluated_results_protected_while_last_actual_pending(self):
        self.seed(95); self.run_hour(96*3600,original=True); self.start()
        with b.database() as c:
            c.execute('UPDATE phase6_events SET actual_return=999 WHERE target=3600')
        with self.assertRaisesRegex(RuntimeError,'Previously evaluated'): b.status()

    def test_phase6b_pages_prestart_and_cohort_isolation_are_readonly(self):
        self.seed(95); self.run_hour(96*3600,original=True)
        client=app.app.test_client()
        before=self.db.read_bytes()
        self.assertIn('Waiting for automatic handoff',client.get('/phase6b/').get_data(as_text=True))
        self.assertFalse(client.get('/phase6b/data.json').get_json()['manifest'])
        self.assertEqual(before,self.db.read_bytes())
        self.start(); self.run_hour(97*3600)
        runtime.evaluate(lambda t:[t,100,110,90,102,1],clock=98*3600)
        with patch.object(phase6_views,'load_candles',return_value={}):
            before=self.db.read_bytes()
            primary=client.get('/direction-active/data.json').get_json()
            extended=client.get('/phase6b/data.json').get_json()
            self.assertEqual(primary['accepted'],96)
            self.assertEqual(extended['accepted'],1)
            self.assertEqual(extended['accepted_limit'],104)
            self.assertEqual(extended['cumulative_accepted'],97)
            self.assertEqual(len(extended['recent_accepted']),1)
            self.assertEqual(extended['recent_accepted'][0]['target'],97*3600)
            self.assertEqual(extended['config'],primary['config'])
            gpt=next(m for m in extended['methods'] if m['method']=='gpt')
            self.assertEqual(gpt['n'],1)
            self.assertAlmostEqual(gpt['mean_signed_pnl'],2)
            for route in ('/phase6b/','/analyze/?phase=phase6b'):
                page=client.get(route)
                self.assertEqual(page.status_code,200)
                html=page.get_data(as_text=True)
                self.assertIn('Phase 6b',html)
                self.assertIn('Accepted / 104',html)
                self.assertIn('Cumulative accepted / 200',html)
                self.assertNotIn('retrospective ACTIVE detector',html)
            self.assertEqual(before,self.db.read_bytes())
        with b.database() as c:
            c.executemany("INSERT INTO phase6b_events(target,started_at,gate) VALUES(?,'start','accepted')",[(i*3600,) for i in range(98,201)])
        self.assertEqual(client.get('/phase6b/data.json').get_json()['status'],'Stopped: fixed accepted-gate limit')

    def test_runtime_parity_only_permitted_changes(self):
        for name in ('volatility_slot','save_method','run','evaluate'):
            original=inspect.getsource(getattr(phase6,name)).replace('phase6_','phase6b_')
            if name=='run':
                original=original.replace('        state = c.execute','        validate(c)\n        state = c.execute',1)
                original=original.replace(">= cfg['stop']['accepted_gates']",'>= ADDITIONAL')
            self.assertEqual(ast.dump(ast.parse(original)),ast.dump(ast.parse(inspect.getsource(getattr(runtime,name)))),name)


def phase6_snapshot(c):
    return {name:[tuple(r) for r in c.execute('SELECT * FROM '+name+' ORDER BY 1,2')]
            for name in ('phase6_config','phase6_events','phase6_calls','phase6_predictions')}


if __name__=='__main__': unittest.main()
