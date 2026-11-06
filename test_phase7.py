import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from phase7_model import quote_pnl,choose,fit,train,frozen_logistic
from phase7_report import report,block_ci
import phase7

class Phase7Tests(unittest.TestCase):
    def test_quotes_use_correct_sides(self):
        e={'best_bid':99.,'best_ask':101.};x={'best_bid':109.,'best_ask':111.}
        self.assertAlmostEqual(quote_pnl('UP',e,x),10000*(109/101-1)-5)
        self.assertAlmostEqual(quote_pnl('DOWN',e,x),10000*(1-111/99)-5)
        self.assertEqual(quote_pnl(None,None,None),0)
        with self.assertRaises(ValueError):quote_pnl('UP',{'best_bid':102,'best_ask':100},x)
    def test_train_requires_full_200(self):
        with self.assertRaises(ValueError):train([])
    def test_frozen_logistic(self):
        from phase6_methods import LOGISTIC_KEYS
        model=dict(coefficients=[0]+[1]*8,mean=[0]*8,scale=[1]*8,training_sha256='test')
        f={k:0 for k in LOGISTIC_KEYS}
        self.assertEqual(frozen_logistic(f,model)['p_up'],.5)
        f[LOGISTIC_KEYS[0]]=None
        self.assertEqual(frozen_logistic(f,model)['status'],'unavailable')
    def test_missing_feature_cash_and_boundary(self):
        rule=dict(feature='flow_strength',threshold=.1,actions=['gpt','logistic'])
        self.assertEqual(choose({'flow_strength':None},rule),'cash')
        self.assertEqual(choose({'flow_strength':.1},rule),'gpt')
        self.assertEqual(choose({'flow_strength':.1001},rule),'logistic')
    def test_training_temporal_purge(self):
        rows=[]
        for i in range(200):
            rows.append(dict(target=i*7200,available_at=i*7200+3700,x=dict(abs_return_1h=i,flow_strength=i,abs_sma_gap=i,gpt_confidence=i),net=dict(gpt=1,order_flow=-1,logistic=0,cash=0)))
        rows[79]['available_at']=rows[80]['target']+1
        result=train(rows)
        self.assertEqual(result['candidates'][0]['folds'][0]['train_n'],79)
        self.assertEqual(result['selector']['actions'],['gpt'])
        self.assertEqual(result['training_n'],200)
        self.assertTrue(all(c['validation_n']==120 for c in result['candidates']))
    def test_readonly_missing_report(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'absent.db'
            self.assertEqual(report(p)['status'],'Not armed');self.assertFalse(p.exists())
    def test_quote_window(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'quotes.db'
            with phase7.database(p) as c:
                c.execute('CREATE TABLE orderbook_samples(timestamp REAL,best_bid REAL,best_ask REAL)')
                c.executemany('INSERT INTO orderbook_samples VALUES(?,?,?)',[(99,1,2),(116,1,2)])
                self.assertIsNone(phase7.first_quote(c,100))
                self.assertEqual(phase7.first_quote(c,101)['timestamp'],116)
    def test_late_and_failed_calls_reserved(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'p.db';phase7.schema(p)
            self.assertIsNone(phase7.call_saved(p,1,'direction',0,lambda:{'p_up':.5},0))
            with phase7.database(p) as c:self.assertEqual(c.execute('SELECT status FROM calls').fetchone()[0],'late')
            with self.assertRaises(sqlite3.IntegrityError):phase7.call_saved(p,1,'direction',0,lambda:{},0)
            def fail():raise ValueError('SECRET MUST NOT LEAK')
            phase7.call_saved(p,2,'direction',0,fail,9999999999)
            with phase7.database(p) as c:
                r=dict(c.execute('SELECT * FROM calls WHERE target=2').fetchone())
                self.assertNotIn('SECRET',json.dumps(r));self.assertEqual(r['error_type'],'ValueError')
    def test_blocks_deterministic_and_zero_days(self):
        daily=[(0,0),(2,1),(4,1),(0,0)]
        self.assertEqual(block_ci(daily,3,200),block_ci(daily,3,200))
        self.assertEqual(block_ci([(3,1)]*10,3,100),[3,3])
    def test_run_before_start_no_calls_and_no_reservation(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'p.db';phase7.schema(p)
            with phase7.database(p) as c:
                c.execute('INSERT INTO config VALUES(1,\'now\',\'{}\',\'x\',\'now\',\'{}\',\'y\',7200,14400)')
            with patch('phase7.validate',return_value={}):
                phase7.run(p,3600,clock=3705,builder=lambda t:self.fail('must not call'))
                phase7.run(p,14400,clock=14505,builder=lambda t:self.fail('must not call'))
            with phase7.database(p) as c:self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],0)
    def test_report_excludes_missing_exit_without_zero_fill(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'p.db';phase7.schema(p)
            frozen=dict(training_n=200,selector={},train_best_fixed='gpt',training_snapshot_hash='a',fit_cutoff=1,logistic={'n':750})
            with phase7.database(p) as c:
                c.execute('INSERT INTO config VALUES(1,\'now\',?,\'x\',\'now\',?,\'y\',3600,5187600)',(json.dumps({'code_sha':'x'}),json.dumps(frozen)))
                c.execute('INSERT INTO events(target,status,started_at,gate,execution_status) VALUES(3600,\'complete\',3705,\'accepted\',\'missing_exit_quote\')')
            r=report(p,clock=7200);self.assertEqual(r['accepted'],1);self.assertEqual(r['executable_n'],0)
            self.assertEqual(r['methods']['selector']['mean_net_bps'],None)
            self.assertEqual(r['primary']['verdict'],'Not tested before fixed end')

    def test_complete_live_cycle_idempotent_and_shared_quotes(self):
        from test_phase6 import snapshot
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'p.db';market=Path(d)/'market.db';candles=Path(d)/'candles.db'
            phase7.schema(p)
            a=dict(market=str(market),candles=str(candles),phase6_design={'gate':{'model':'fake','volatility_config':{}}},code_sha='test')
            learned=dict(selector={'feature':None,'actions':['gpt']},train_best_fixed='logistic',
                logistic=dict(coefficients=[0]+[0]*8,mean=[0]*8,scale=[1]*8,training_sha256='test',n=200),
                training_n=200,training_snapshot_hash='x',fit_cutoff=0)
            with phase7.database(p) as c:
                c.execute('INSERT INTO config VALUES(1,?,?,?,?,?,?,?,?)',('now',json.dumps(a),'a','now',json.dumps(learned),'b',3600,5187600))
            with phase7.database(market) as c:
                c.execute('CREATE TABLE orderbook_samples(timestamp REAL,best_bid REAL,best_ask REAL)')
                c.executemany('INSERT INTO orderbook_samples VALUES(?,?,?)',[(3721,100,101),(7201,110,111)])
            with phase7.database(candles) as c:
                c.execute('CREATE TABLE candles_5m(start_ts INTEGER,close REAL)')
                c.execute('INSERT INTO candles_5m VALUES(6900,110)')
            with patch('phase7.validate',return_value=a),patch('phase7.time.time',return_value=3720):
                phase7.run(p,3600,builder=lambda t:(snapshot(t),0),class_call=lambda s:([0,0,1],'fake','{}'),gpt_call=lambda *a:phase7.output(p_up=.8))
                phase7.run(p,3600,builder=lambda t:self.fail('duplicate call'))
            with phase7.database(p) as c:
                e=dict(c.execute('SELECT * FROM events').fetchone())
                self.assertEqual(e['gate'],'accepted');self.assertEqual(e['selection'],'gpt')
                self.assertEqual(c.execute('SELECT count(*) FROM calls').fetchone()[0],12)
            with patch('phase7.validate',return_value=a):phase7.evaluate(p,clock=7300)
            r=report(p,clock=7300)
            self.assertEqual(r['executable_n'],1)
            self.assertAlmostEqual(r['methods']['selector']['mean_net_bps'],10000*(110/101-1)-5)
            self.assertEqual(r['methods']['selector'],r['methods']['gpt'])
            self.assertEqual(r['methods']['cash']['trades'],0)
            before=p.read_bytes();report(p,clock=7300);self.assertEqual(before,p.read_bytes())

    def test_public_routes_do_not_initialize(self):
        import app
        with tempfile.TemporaryDirectory() as d,patch.object(app,'DB_PATH',Path(d)/'btc.db'):
            client=app.app.test_client()
            self.assertEqual(client.get('/phase7/').status_code,200)
            self.assertEqual(client.get('/phase7/data.json').json['status'],'Not armed')
            self.assertFalse((Path(d)/'phase7.db').exists())

    def test_window_and_code_hash_tampering_fails_closed(self):
        from phase4 import config_hash
        a=dict(source_hashes={'x':'one'},dependencies={'numpy':'one'},days=60)
        f=dict(fit_cutoff=120)
        state=dict(arm_json=json.dumps(a),arm_hash=config_hash(a),frozen_json=json.dumps(f),frozen_hash=config_hash(f),start_target=3600,end_exclusive=3600+60*86400)
        with patch('phase7.hashes',return_value={'x':'one'}),patch('phase7.dependencies',return_value={'numpy':'one'}):
            phase7.validate(state)
            state['end_exclusive']+=3600
            with self.assertRaises(RuntimeError):phase7.validate(state)
        state['end_exclusive']-=3600
        with patch('phase7.hashes',return_value={'x':'two'}),patch('phase7.dependencies',return_value={'numpy':'one'}):
            with self.assertRaises(RuntimeError):phase7.validate(state)

if __name__=='__main__':unittest.main()
