import json
import math
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock,patch

import app
import candle_store
import claude_observation as original_claude
import phase5b
import phase6
import phase6d as d
from phase4 import config_hash
from phase6d_analysis import report
import phase6d_views
from test_phase6 import snapshot


class Phase6dTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.side=self.root/'phase6d.db';self.source=self.root/'btc.db'
        self.claude=self.root/'claude-observation.db';self.candles=self.root/'candles.db'
        self.t=d.END-4*3600
        self.context=snapshot(self.t)
        self.vol={'quiet_upper':.2,'active_lower':.5}
        with patch.object(phase6,'DB_PATH',self.source):phase6.migrate()
        phase5b.migrate(self.source);original_claude.migrate(self.claude)
        frozen={'gate':{'volatility_config':self.vol}}
        with original_claude.db(self.source) as c:
            c.execute('INSERT INTO phase6_config VALUES(1,?,?,?,?,?)',
                      (json.dumps(frozen),config_hash(frozen),'now',self.t,'original-sha'))
            c.execute('INSERT INTO phase5b_config VALUES(1,?,?,?,?,?,?,?)',
                      ('now',self.t,self.t+3600,'phase5b-v1','original-sha','{}','dummy'))
        with patch.object(d.subprocess,'check_output',side_effect=['new-sha','']):
            self.state=d.initialize(self.side,self.source,self.claude,self.candles,clock=self.t-1)
        self.labels=deque();self.label_lock=threading.Lock()
        self.call=Mock(side_effect=self.response)
        self.builder=Mock(return_value=(self.context,.1))
        self.network=patch.object(socket.socket,'connect',side_effect=AssertionError('No network in tests'))
        self.network.start();self.addCleanup(self.network.stop)

    def response(self,kind,context,cfg,timeout):
        if kind=='class':
            with self.label_lock:label=self.labels.popleft() if self.labels else 'ACTIVE'
            p={'ACTIVE':[.05,.05,.9],'NORMAL':[.1,.8,.1],'QUIET':[.8,.1,.1]}[label]
            value=dict(zip(('p_quiet','p_normal','p_active'),p))
        else:value={'p_up':.7,'p_down':.3}
        return {'model':'mock-opus','stop_reason':'end_turn','content':[{'type':'thinking','thinking':'','signature':'mock'},
                {'type':'text','text':json.dumps(value)}]}

    def seed(self,target=None,gpt_gate='rejected',gpt_class='NORMAL'):
        target=self.t if target is None else target
        text=snapshot(target)
        with original_claude.db(self.source) as c:
            c.execute('''INSERT INTO phase5b_predictions(target_candle_time,started_at,status,version,context,snapshot_hash)
                VALUES(?,'now','complete','v',?,?)''',(target,text,d.digest(text)))
            c.execute('''INSERT INTO phase6_events(target,started_at,context,snapshot_hash,reference_close,gate,single_class,gate_json)
                VALUES(?,'now',?,?,100,?,?,?)''',
                (target,text,d.digest(text),gpt_gate,gpt_class,json.dumps({'repeats':['ACTIVE']*10 if gpt_class=='ACTIVE' else []})))
        return text

    def cycle(self,clock=None,call=None,builder=None):
        d.cycle(self.side,self.source,self.claude,self.candles,clock_fn=lambda:self.t+150 if clock is None else clock,
                call=self.call if call is None else call,builder=self.builder if builder is None else builder)

    def rows(self,table):
        with original_claude.db(self.side,True) as c:return [dict(r) for r in c.execute('SELECT * FROM '+table)]

    def snapshot_dbs(self):
        result=[]
        for path in (self.source,self.claude):
            with sqlite3.connect(path) as c:result.append('\n'.join(c.iterdump()))
        return result

    def truth(self,flat=False):
        candle_store.initialize(self.candles,self.t-300)
        rows=[[self.t-300+i*300,90,120,100,100 if flat else 100*1.003**i,1] for i in range(13)]
        candle_store.save(self.candles,rows,self.t+3700)
        d.evaluate(self.side,self.candles,json.loads(self.state['config_json']),self.t+3700)
        return rows

    def test_init_is_future_frozen_and_idempotent(self):
        self.assertEqual(self.state['start_target'],self.t)
        self.assertEqual(d.initialize(self.side,self.source,self.claude,self.candles,clock=self.t+7200),self.state)
        with patch.dict(d.PARAMS,max_tokens=123):
            with self.assertRaises(RuntimeError):self.cycle()
        with self.assertRaises(ValueError):d.initialize(self.source,self.source,self.claude,self.candles)
        with self.assertRaises(ValueError):d.initialize(self.root/'expired.db',self.source,self.claude,self.candles,clock=d.END)

    def test_independent_accepted_when_gpt_rejected_and_source_unchanged(self):
        self.seed(gpt_gate='rejected',gpt_class='NORMAL');before=self.snapshot_dbs()
        self.labels.extend(['ACTIVE']*9+['NORMAL']*2)
        self.cycle();self.cycle()
        event=self.rows('phase6d_events')[0]
        self.assertEqual(event['gate'],'accepted');self.assertEqual(event['active_votes'],8)
        self.assertEqual(self.call.call_count,12)
        self.assertEqual(event['snapshot_hash'],d.digest(self.context))
        self.assertEqual(before,self.snapshot_dbs());self.builder.assert_not_called()
        self.assertTrue(all(args.args[1]==self.context for args in self.call.call_args_list))

    def test_claude_nonactive_rejects_even_if_gpt_accepted(self):
        self.seed(gpt_gate='accepted',gpt_class='ACTIVE');self.labels.append('QUIET')
        self.cycle()
        self.assertEqual(self.call.call_count,1)
        self.assertEqual(self.rows('phase6d_events')[0]['gate'],'rejected')
        self.assertFalse(any(r['kind']=='direction' for r in self.rows('phase6d_calls')))

    def test_seven_votes_not_accepted(self):
        self.seed();self.labels.extend(['ACTIVE']*8+['NORMAL']*3);self.cycle()
        event=self.rows('phase6d_events')[0]
        self.assertEqual(event['active_votes'],7);self.assertEqual(event['gate'],'rejected')
        self.assertEqual(self.call.call_count,11)

    def test_partial_repeat_failures_do_not_generate_direction(self):
        self.seed();original=self.response
        counter=[0]
        def fail_one(kind,context,cfg,timeout):
            with self.label_lock:
                counter[0]+=1;n=counter[0]
            if n==5:raise ValueError('private secret must not persist')
            return original(kind,context,cfg,timeout)
        self.cycle(call=fail_one)
        self.assertEqual(self.rows('phase6d_events')[0]['gate'],'unknown')
        self.assertEqual(len(self.rows('phase6d_calls')),11)
        self.assertNotIn(b'private secret',self.side.read_bytes())

    def test_single_failure_next_hour_continues(self):
        self.seed();self.call.side_effect=RuntimeError('not logged')
        self.cycle();self.cycle();self.assertEqual(self.call.call_count,1)
        self.seed(self.t+3600);self.call.side_effect=self.response
        self.cycle(self.t+3600+150)
        self.assertEqual(self.call.call_count,13)
        self.assertEqual([r['gate'] for r in self.rows('phase6d_events')],['unknown','accepted'])

    def test_direction_failure_does_not_change_accepted_gate(self):
        self.seed()
        def fail_direction(kind,context,cfg,timeout):
            if kind=='direction':raise ValueError('provider unavailable')
            return self.response(kind,context,cfg,timeout)
        self.cycle(call=fail_direction)
        self.assertEqual(self.rows('phase6d_events')[0]['gate'],'accepted')
        self.assertEqual(self.rows('phase6d_calls')[-1]['status'],'failed')

    def test_wait_for_input_no_rebuilding_inside_5b_window(self):
        self.cycle();self.call.assert_not_called();self.builder.assert_not_called()
        self.seed();self.cycle();self.assertEqual(self.call.call_count,12)

    def test_exact_5c_input_wins_and_bad_hash_is_terminal(self):
        self.seed()
        with original_claude.db(self.claude) as c:
            c.execute('INSERT INTO phase5c_predictions(target,status,started_at,context,snapshot_hash) VALUES(?,\'failed\',\'now\',?,?)',
                      (self.t,self.context,d.digest(self.context)))
        self.cycle();self.assertEqual(self.rows('phase6d_events')[0]['snapshot_source'],'phase5c')
        self.seed(self.t+3600)
        with original_claude.db(self.claude) as c:
            c.execute('INSERT INTO phase5c_predictions(target,status,started_at,context,snapshot_hash) VALUES(?,\'complete\',\'now\',?,\'wrong\')',
                      (self.t+3600,self.context))
        self.cycle(self.t+3600+150)
        self.assertEqual(self.call.call_count,12)
        self.assertEqual(self.rows('phase6d_events')[-1]['status'],'failed')

    def test_builder_outside_5b_window_and_deadline(self):
        target=self.t+7200
        self.builder.return_value=(snapshot(target),.1)
        self.cycle(target+150);self.builder.assert_called_once_with(target)
        self.assertEqual(self.call.call_count,12)
        self.cycle(d.END+3600);self.assertEqual(self.call.call_count,12)
        self.assertEqual(len(self.rows('phase6d_events')),4)

    def test_missed_and_crash_reservations_no_retry(self):
        self.seed();self.cycle(self.t+600);self.call.assert_not_called()
        self.seed(self.t+3600)
        with original_claude.db(self.side) as c:
            c.execute("INSERT INTO phase6d_events(target,status,started_at) VALUES(?,'started','now')",(self.t+3600,))
        self.cycle(self.t+3600+150);self.call.assert_not_called()
        self.assertEqual(self.rows('phase6d_events')[-1]['status'],'interrupted')

    def test_concurrent_workers_only_one_calls_each_slot(self):
        self.seed()
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:self.cycle(),range(2)))
        self.assertEqual(self.call.call_count,12)

    def test_late_response_and_no_direction_after_deadline(self):
        self.seed();clock=[self.t+150]
        def late(kind,context,cfg,timeout):
            clock[0]=self.t+601
            return self.response(kind,context,cfg,timeout)
        d.cycle(self.side,self.source,self.claude,self.candles,clock_fn=lambda:clock[0],call=late)
        self.assertEqual(self.rows('phase6d_calls')[0]['status'],'late')
        self.assertEqual(self.rows('phase6d_events')[0]['gate'],'unknown')
        self.assertEqual(len(self.rows('phase6d_calls')),1)

    def test_late_direction_is_visible_but_unscored(self):
        self.seed();clock=[self.t+150]
        def late_direction(kind,context,cfg,timeout):
            if kind=='direction':clock[0]=self.t+601
            return self.response(kind,context,cfg,timeout)
        d.cycle(self.side,self.source,self.claude,self.candles,clock_fn=lambda:clock[0],call=late_direction)
        self.truth()
        result=report(self.side)
        self.assertEqual(result['gates']['accepted'],1)
        self.assertEqual(result['direction']['n'],0)
        self.assertEqual(result['recent'][0]['direction_status'],'late')
        self.assertIsNone(result['recent'][0]['brier'])

    def test_invalid_probabilities_and_truncated_thinking_response(self):
        for value in ({'p_quiet':True,'p_normal':0,'p_active':0},
                      {'p_quiet':.3,'p_normal':.3,'p_active':.3},
                      {'p_quiet':float('nan'),'p_normal':0,'p_active':1}):
            raw=self.response('class','',{},1);raw['content'][-1]['text']=json.dumps(value)
            with self.assertRaises(ValueError):d.parse_class(raw)
        raw=self.response('class','',{},1);raw['stop_reason']='max_tokens'
        with self.assertRaises(ValueError):d.parse_class(raw)

    def test_gpt_class_prompt_exact_parity_and_direction_prompt_same(self):
        import next_stages
        cfg=json.loads(self.state['config_json'])
        result=Mock(output_text='{"p_quiet":0.1,"p_normal":0.1,"p_active":0.8}',model='mock')
        result.model_dump_json.return_value='{}'
        source={'config_json':json.dumps(self.vol),'predictor':'openai','model_version':'mock','context':self.context}
        with patch('openai.OpenAI') as client:
            client.return_value.responses.create.return_value=result
            next_stages.class_call(source)
            self.assertEqual(client.return_value.responses.create.call_args.kwargs['input'],
                             original_claude.request_text('class',self.context,cfg))
        self.assertEqual(cfg['prompts']['direction'],original_claude.prompt_literal('phase6.py','direction_call'))

    def test_actual_rv_reference_direction_and_missing_data(self):
        self.seed();self.cycle()
        d.evaluate(self.side,self.candles,json.loads(self.state['config_json']),self.t+3700)
        self.assertIsNone(self.rows('phase6d_events')[0]['evaluated_at'])
        candles=self.truth();event=self.rows('phase6d_events')[0]
        expected_rv=100*math.sqrt(sum(math.log(b[4]/a[4])**2 for a,b in zip(candles,candles[1:])))
        self.assertAlmostEqual(event['actual_rv'],expected_rv)
        self.assertEqual(event['actual_class'],'ACTIVE');self.assertEqual(event['actual_direction'],'UP')
        direction=next(r for r in self.rows('phase6d_calls') if r['kind']=='direction')
        self.assertEqual(direction['correct'],1);self.assertAlmostEqual(direction['brier'],.09)
        self.assertAlmostEqual(direction['signed_pnl'],100*(candles[-1][4]/100-1))
        self.assertEqual(self.call.call_count,12)
        r=report(self.side)
        self.assertEqual(r['active_detection']['tp'],1)
        self.assertEqual(r['gate_overlap']['claude_only_accepted'],1)
        self.assertEqual(r['direction']['n'],1)

    def test_flat_outcomes(self):
        self.seed();self.cycle();self.truth(flat=True)
        direction=next(r for r in self.rows('phase6d_calls') if r['kind']=='direction')
        self.assertIsNone(direction['correct']);self.assertIsNone(direction['brier']);self.assertEqual(direction['signed_pnl'],0)
        self.assertEqual(report(self.side)['active_detection']['fp'],1)

    def test_four_way_common_direction_and_snapshot_mismatch_exclusion(self):
        self.seed(gpt_gate='accepted',gpt_class='ACTIVE');self.cycle();self.truth()
        e=self.rows('phase6d_events')[0];ret=e['actual_return']
        with original_claude.db(self.source) as c:
            c.execute('UPDATE phase6_events SET actual_return=?,actual_direction=\'UP\',evaluated_at=\'now\' WHERE target=?',(ret,self.t))
            for method in ('gpt','logistic'):
                c.execute('''INSERT INTO phase6_predictions(target,method,status,started_at,direction,p_up,p_down,version,correct,brier,signed_pnl)
                    VALUES(?,?,'complete','now','UP',.7,.3,'v',1,.09,?)''',(self.t,method,ret))
        with original_claude.db(self.claude) as c:
            c.execute('INSERT INTO phase6c_events VALUES(\'phase6\',?,?,?,\'{}\',\'UP\',?,\'now\')',
                      (self.t,self.context,d.digest(self.context),ret))
            c.execute('''INSERT INTO phase6c_calls(cohort,target,slot,status,started_at,direction,p_up,p_down,correct,brier,signed_pnl)
                VALUES('phase6',?,0,'complete','now','UP',.7,.3,1,.09,?)''',(self.t,ret))
        r=report(self.side)
        self.assertEqual(r['common_direction']['common_target_n'],1)
        self.assertEqual(r['gate_overlap']['both_accepted'],1)
        with original_claude.db(self.source) as c:c.execute("UPDATE phase6_events SET snapshot_hash='other'")
        self.assertEqual(report(self.side)['common_direction']['common_target_n'],0)

    def test_read_only_views_routes_and_no_secret_fields(self):
        self.seed();self.cycle();self.truth()
        with original_claude.db(self.side) as c:
            c.execute("UPDATE phase6d_calls SET raw_json='secret raw'")
            c.execute("UPDATE phase6d_events SET reason='<unsafe>'")
        before=self.side.read_bytes()
        with patch.object(app,'DB_PATH',self.source), patch.object(d,'cycle',side_effect=AssertionError('No cycle')), \
             patch.object(d,'initialize',side_effect=AssertionError('No init')):
            client=app.app.test_client()
            for url in ('/phase6d/','/phase6d/data.json','/analyze/?phase=phase6d'):
                response=client.get(url);self.assertEqual(response.status_code,200)
                text=response.get_data(as_text=True)
                self.assertNotIn('secret raw',text);self.assertNotIn(self.context,text)
                self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertIn('&lt;unsafe&gt;',client.get('/phase6d/').get_data(as_text=True))
        self.assertEqual(self.side.read_bytes(),before)

    def test_missing_view_does_not_create_database(self):
        self.side.unlink()
        self.assertEqual(phase6d_views.report(self.source)['status'],'unavailable')
        self.assertFalse(self.side.exists())


if __name__=='__main__':unittest.main()
