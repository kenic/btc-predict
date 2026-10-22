import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import claude_observation as o
from claude_analysis import report


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.side=self.root/'side.db'; self.source=self.root/'source.db'; self.candles=self.root/'candles.db'
        self.t=o.END-3*3600
        self.ctx='Realized volatility class of the next completed 1-hour candle. exact snapshot'
        with o.db(self.source) as c:
            c.executescript('''CREATE TABLE phase5b_config(start_target INTEGER,end_target INTEGER);
            CREATE TABLE phase5b_predictions(target_candle_time INTEGER PRIMARY KEY,context TEXT,
              snapshot_hash TEXT, previous_rv REAL,predicted_rv REAL,actual_rv REAL,status TEXT);''')
            c.execute('INSERT INTO phase5b_config VALUES(?,?)',(self.t,self.t+3600))
            for name in ('phase6','phase6b'):
                c.execute(f'''CREATE TABLE {name}_events(target INTEGER PRIMARY KEY,gate TEXT,context TEXT,snapshot_hash TEXT,
                  gate_json TEXT,actual_direction TEXT,actual_return REAL,evaluated_at TEXT)''')
                c.execute(f'''CREATE TABLE {name}_predictions(target INTEGER,method TEXT,status TEXT,direction TEXT,
                  p_up REAL,p_down REAL,correct INTEGER,brier REAL,signed_pnl REAL)''')
        self.state=o.initialize(self.side,self.source,self.candles,clock=self.t-1)
        self.call=Mock(side_effect=self.response)
        self.builder=Mock(return_value=(self.ctx,.1))
        # Any accidental real network connection fails this suite.
        self.network=patch.object(socket.socket,'connect',side_effect=AssertionError('No network in tests'))
        self.network.start(); self.addCleanup(self.network.stop)

    def response(self,kind,context,cfg,timeout):
        v={'predicted_rv':.3} if kind=='rv' else {'p_up':.7,'p_down':.3}
        return dict(model='mock-claude',stop_reason='end_turn',content=[{'type':'text','text':json.dumps(v)}])

    def seed(self,cohort='phase6',gate='accepted',target=None):
        t=self.t if target is None else target
        with o.db(self.source) as c:
            c.execute('INSERT OR REPLACE INTO phase5b_predictions VALUES(?,?,?,?,?,?,?)',(t,self.ctx,o.digest(self.ctx),.1,.2,None,'complete'))
            c.execute(f'INSERT INTO {cohort}_events VALUES(?,?,?,?,?,?,?,?)',(t,gate,self.ctx,o.digest(self.ctx),'{}',None,None,None))

    def cycle(self,clock=None):
        o.cycle(self.side,self.source,self.candles,clock_fn=lambda:self.t+150 if clock is None else clock,call=self.call,builder=self.builder)

    def rows(self,table):
        with o.db(self.side) as c:
            return [dict(r) for r in c.execute('SELECT * FROM '+table)]

    def test_init_frozen_and_expired(self):
        self.assertEqual(o.initialize(self.side,self.source,self.candles,clock=self.t+600),self.state)
        with patch.dict(o.PARAMS,temperature=.5):
            with self.assertRaises(RuntimeError): self.cycle()
        with self.assertRaises(ValueError): o.initialize(self.root/'expired.db',self.source,self.candles,clock=o.END)
        with self.assertRaises(ValueError): o.initialize(self.source,self.source,self.candles,clock=self.t)

    def test_prestart_refreeze_preserves_boundary_audits_and_is_idempotent(self):
        old=json.loads(self.state['config_json'])
        old['version']='claude-side-v1'
        old['params']={'model':'claude-sonnet-4-6','temperature':1,'max_tokens':1024,'thinking':{'type':'disabled'}}
        frozen=json.dumps(old,sort_keys=True)
        with o.db(self.side) as c:
            c.execute('UPDATE claude_config SET config_json=?,config_hash=?',(frozen,o.digest(frozen)))
        first=o.refreeze_before_start(self.side,self.source,self.candles,clock=self.t-1)
        self.assertEqual(first['start_target'],self.state['start_target'])
        self.assertEqual(first['end_exclusive'],self.state['end_exclusive'])
        self.assertEqual(json.loads(first['config_json'])['params']['model'],'claude-opus-5-5')
        self.assertEqual(o.refreeze_before_start(self.side,self.source,self.candles,clock=self.t-1),first)
        with o.db(self.side) as c:
            history=c.execute('SELECT * FROM claude_config_history').fetchall()
            self.assertEqual(len(history),1)
            self.assertEqual(json.loads(history[0]['old_registration_json'])['config_json'],frozen)
        with self.assertRaises(RuntimeError):
            o.refreeze_before_start(self.side,self.source,self.candles,clock=self.t)
        with o.db(self.side) as c:
            c.execute("INSERT INTO phase5c_predictions(target,status,started_at) VALUES(?,'started','now')",(self.t,))
        with self.assertRaises(RuntimeError):
            o.refreeze_before_start(self.side,self.source,self.candles,clock=self.t-1)

    def test_thinking_blocks_are_ignored_and_truncation_rejected(self):
        raw=self.response('direction','',{},1)
        raw['content'].insert(0,{'type':'thinking','thinking':'','signature':'mock'})
        self.assertEqual(o.parse('direction',raw)['direction'],'UP')
        raw['stop_reason']='max_tokens'
        with self.assertRaises(ValueError): o.parse('direction',raw)

    def test_primary_ten_repeats_exact_snapshot_idempotent_source_unchanged(self):
        self.seed()
        before=self.source.read_bytes()
        self.cycle(); self.cycle()
        self.assertEqual(self.call.call_count,12)
        self.assertEqual(self.source.read_bytes(),before)
        self.assertEqual(len(self.rows('phase6c_calls')),11)
        self.assertEqual({r['status'] for r in self.rows('phase6c_calls')},{'complete'})
        self.builder.assert_not_called()
        self.assertTrue(all(a.args[1]==self.ctx for a in self.call.call_args_list))
        self.assertEqual(self.rows('phase5c_predictions')[0]['snapshot_hash'],o.digest(self.ctx))

    def test_rejected_no_direction_but_rv(self):
        self.seed(gate='rejected'); self.cycle()
        self.assertEqual(self.call.call_count,1)
        self.assertFalse(self.rows('phase6c_calls'))

    def test_6b_and_wait_for_gate(self):
        self.seed(cohort='phase6b',gate='pending'); self.cycle()
        self.assertEqual(self.call.call_count,1)
        with o.db(self.source) as c: c.execute("UPDATE phase6b_events SET gate='accepted'")
        self.cycle()
        self.assertEqual(self.call.call_count,12)
        self.assertEqual(self.rows('phase6c_events')[0]['cohort'],'phase6b')

    def test_wait_for_exact_5b_input_no_rebuild(self):
        self.cycle(); self.call.assert_not_called(); self.builder.assert_not_called()
        self.seed(); self.cycle(); self.assertEqual(self.call.call_count,12)

    def test_outside_5b_window_uses_same_builder(self):
        self.cycle(clock=self.t+2*3600+150)
        self.assertEqual(self.call.call_count,1)
        self.builder.assert_called_once_with(self.t+2*3600)

    def test_failures_do_not_retry_and_do_not_stop_next_hour(self):
        self.seed(); self.call.side_effect=RuntimeError('secret-do-not-save')
        self.cycle(); self.cycle(); self.assertEqual(self.call.call_count,12)
        self.assertNotIn(b'secret-do-not-save',self.side.read_bytes())
        self.seed(target=self.t+3600); self.call.side_effect=self.response
        self.cycle(clock=self.t+3600+150); self.assertEqual(self.call.call_count,24)

    def test_late_and_expiry_no_backfill_no_replacement(self):
        self.seed(); self.cycle(clock=self.t+600)
        self.call.assert_not_called()
        self.assertTrue(all(r['status']=='missed' for r in self.rows('phase6c_calls')))
        self.cycle(clock=o.END+7200)
        self.call.assert_not_called()
        self.assertEqual(len(self.rows('phase5c_predictions')),3)

    def test_late_response_excluded(self):
        self.seed()
        with o.db(self.side) as c:
            c.execute("INSERT INTO phase5c_predictions(target,status,started_at) VALUES(?,'started','now')",(self.t,))
        clock=Mock(side_effect=[self.t+150,self.t+150,self.t+601])
        o.invoke(self.side,'phase5c_predictions',{'target':self.t},'rv',self.ctx,o.spec(),clock,self.call)
        self.assertEqual(self.rows('phase5c_predictions')[0]['status'],'late')

    def test_crash_reservations_never_retried(self):
        self.seed(); self.cycle()
        with o.db(self.side) as c:
            c.execute("UPDATE phase6c_calls SET status='started'")
            c.execute("UPDATE phase5c_predictions SET status='started'")
        self.call.reset_mock(); self.cycle(); self.call.assert_not_called()

    def test_concurrent_cycles_only_one_request_per_slot(self):
        from concurrent.futures import ThreadPoolExecutor
        self.seed()
        with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(lambda _:self.cycle(),range(2)))
        self.assertEqual(self.call.call_count,12)

    def test_invalid_values(self):
        for kind,v in [('rv',{'predicted_rv':-1}),('rv',{'predicted_rv':True}),('rv',{'predicted_rv':float('nan')}),
                       ('direction',{'p_up':.7,'p_down':.7}),('direction',{'p_up':True,'p_down':0})]:
            raw=self.response(kind,'',{},1); raw['content'][0]['text']=json.dumps(v)
            with self.assertRaises(ValueError): o.parse(kind,raw)
        raw=self.response('rv','',{},1); raw['stop_reason']='max_tokens'
        with self.assertRaises(ValueError): o.parse('rv',raw)

    def test_prompt_exact_equality_with_gpt(self):
        import next_stages, phase6
        response=Mock(output_text='{"predicted_rv":0.3}',model='mock')
        response.model_dump_json.return_value='{}'
        with patch('openai.OpenAI') as client:
            client.return_value.responses.create.return_value=response
            next_stages.regression_call(self.ctx)
            self.assertEqual(client.return_value.responses.create.call_args.kwargs['input'],o.request_text('rv',self.ctx,o.spec()))
            response.output_text='{"p_up":0.7,"p_down":0.3}'
            phase6.direction_call(self.ctx,'mock')
            self.assertEqual(client.return_value.responses.create.call_args.kwargs['input'],o.request_text('direction',self.ctx,o.spec()))

    def test_provider_payload_and_secret_redaction(self):
        response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps(dict(self.response('rv','',{},1),extra='test-secret')).encode()
        with patch.dict(os.environ,ANTHROPIC_API_KEY='test-secret'),patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=response
            raw=o.provider('rv',self.ctx,o.spec(),2)
            request=opener.return_value.open.call_args.args[0]
            payload=json.loads(request.data)
            self.assertEqual(payload['model'],o.MODEL)
            self.assertNotIn('temperature',payload)
            self.assertEqual(payload['thinking'],{'type':'adaptive'})
            self.assertEqual(payload['output_config'],{'effort':'medium'})
            self.assertEqual(payload['max_tokens'],4096)
            self.assertEqual(payload['messages'][0]['content'],o.request_text('rv',self.ctx,o.spec()))
            self.assertNotIn('test-secret',json.dumps(raw))
            opener.return_value.open.assert_called_once()

    def test_evaluation_and_analysis_common_and_repeat_tie(self):
        self.seed(); self.cycle()
        with o.db(self.source) as c:
            c.execute("UPDATE phase6_events SET actual_return=-2,actual_direction='DOWN',evaluated_at='done'")
            c.execute("INSERT INTO phase6_predictions VALUES(?,'gpt','complete','DOWN',.2,.8,1,.04,2)",(self.t,))
            c.execute("INSERT INTO phase6_predictions VALUES(?,'logistic','complete','UP',.6,.4,0,.36,-2)",(self.t,))
            c.execute('UPDATE phase5b_predictions SET actual_rv=.4')
        with o.db(self.side) as c:
            c.execute("UPDATE phase5c_predictions SET actual_rv=.4,evaluated_at='done'")
            c.execute("UPDATE phase6c_calls SET direction='DOWN',p_up=.3,p_down=.7 WHERE slot BETWEEN 1 AND 5")
        o.evaluate(self.side,self.source,self.candles,self.t+3700)
        r=report(self.side,self.source)
        self.assertEqual(r['direction']['common_target_n'],1)
        self.assertAlmostEqual(r['direction']['methods']['claude']['brier'],.49)
        self.assertEqual(r['direction']['methods']['claude']['total_signed_pnl'],-2)
        self.assertAlmostEqual(r['rv']['claude']['mae'],.1)
        self.assertEqual(r['same_different']['different']['n'],1)
        self.assertEqual(r['repeat_cases'][0]['agreement'],.5)
        self.assertIsNone(r['repeat_cases'][0]['modal_direction'])
        self.assertEqual(r['repeat_agreement_performance']['0.5']['modal']['n'],0)
        with o.db(self.source) as c: c.execute("UPDATE phase6_predictions SET status='unavailable' WHERE method='logistic'")
        self.assertEqual(report(self.side,self.source)['direction']['common_target_n'],0)

    def test_canonical_rv_and_missing_candles_retry_without_api(self):
        import candle_store
        import volatility
        self.seed(); self.cycle()
        o.evaluate(self.side,self.source,self.candles,self.t+3700)
        self.assertIsNone(self.rows('phase5c_predictions')[0]['actual_rv'])
        candle_store.initialize(self.candles,self.t-300)
        candles=[[self.t-300+i*300,90,120,100,100+i*.1,1] for i in range(13)]
        candle_store.save(self.candles,candles,self.t+3700)
        o.evaluate(self.side,self.source,self.candles,self.t+3700)
        self.assertAlmostEqual(self.rows('phase5c_predictions')[0]['actual_rv'],
                               volatility.realized_volatility(candles,self.t))
        self.assertEqual(self.call.call_count,12)

    def test_snapshot_mismatch_rejected_and_analysis_does_not_pair(self):
        self.seed(); self.cycle()
        with o.db(self.source) as c:
            c.execute("UPDATE phase6_events SET snapshot_hash='wrong'")
        with self.assertRaises(RuntimeError): self.cycle()
        self.assertEqual(self.call.call_count,12)

    def test_missing_key_no_network_and_no_api_attempt(self):
        self.seed()
        with patch.dict(os.environ,{},clear=True):
            o.cycle(self.side,self.source,self.candles,clock_fn=lambda:self.t+150,call=o.provider)
        self.assertTrue(all(r['api_started_at'] is None for r in self.rows('phase6c_calls')))
        self.assertTrue(all(r['status']=='failed' for r in self.rows('phase6c_calls')))

    def test_flat_and_incomplete_repeats(self):
        self.seed(); self.cycle()
        with o.db(self.source) as c:
            c.execute("UPDATE phase6_events SET actual_return=0,actual_direction='FLAT',evaluated_at='done'")
        with o.db(self.side) as c: c.execute("UPDATE phase6c_calls SET status='failed' WHERE slot=10")
        o.evaluate(self.side,self.source,self.candles,self.t+3700)
        primary=self.rows('phase6c_calls')[0]
        self.assertIsNone(primary['correct']); self.assertIsNone(primary['brier']); self.assertEqual(primary['signed_pnl'],0)
        r=report(self.side,self.source)
        self.assertIsNone(r['repeat_cases'][0]['agreement'])
        self.assertFalse(r['repeat_agreement_performance'])


if __name__=='__main__': unittest.main()
