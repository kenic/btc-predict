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
from test_phase6 import snapshot


class Phase6bTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/'test.db'
        self.patches = [patch.object(m,'DB_PATH',self.db) for m in (phase4,next_stages,phase6,b)]
        for p in self.patches: p.start()
        phase4.init_schema(); next_stages.migrate(); phase6.migrate()
        cfg = phase6.design({'quiet_upper':.2,'active_lower':.4},'test-model')
        cfg['implementation_hashes'] = phase6.implementation_hashes()
        with b.database() as c:
            c.execute('INSERT INTO phase6_config VALUES(1,?,?,?,?,?)',
                (json.dumps(cfg),phase4.config_hash(cfg),'start',3600,'original'))
        self.calls = []

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
        (phase6 if original else runtime).run(target,**defaults)

    def test_95_96_97_closure_and_primary_immutability(self):
        self.seed(95)
        with self.assertRaisesRegex(RuntimeError,'exactly 96'): self.start()
        self.run_hour(96*3600,original=True)
        self.assertEqual(len(self.calls),11)
        self.run_hour(97*3600,original=True)
        self.assertEqual(len(self.calls),11)  # Original really stops at96.
        with self.assertRaisesRegex(RuntimeError,'all 96'): self.start()
        phase6.evaluate(lambda t:[t,100,110,90,101,1],clock=97*3600)
        with b.database() as c: before=phase6_snapshot(c)
        first=self.start()
        self.assertEqual(first['runtime_config']['start_target'],97*3600)
        self.assertEqual(first,b.start(clock=999999))
        self.run_hour(96*3600)  # No old-target backfill.
        self.assertEqual(len(self.calls),11)
        self.run_hour(97*3600)
        self.assertEqual(len(self.calls),22)
        runtime.evaluate(lambda t:[t,100,110,90,102,1],clock=98*3600)
        with b.database() as c:
            self.assertEqual(before,phase6_snapshot(c))
            row=c.execute("SELECT * FROM phase6b_predictions WHERE method='always_up'").fetchone()
            self.assertAlmostEqual(row['signed_pnl'],2)
            self.assertEqual(row['correct'],1)
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
        with self.assertRaisesRegex(RuntimeError,'not been explicitly'): self.run_hour(97*3600)
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
