import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from tests_phase8a.test_phase8a import Phase8aTests, T
from phase8a import pilot as baseline
from phase8b import technical, pilot, scheduled, adapters, market, store
from phase8b.core import SYMBOLS, canonical, digest


class Phase8bTests(Phase8aTests):
    def setup_pair(self):
        self.live_config();self.ingest();self.candles_for()
        self.baseline=self.path/'baseline';self.baseline.mkdir()
        with patch('phase8a.adapters.preflight'):
            plans,ceiling=baseline.prepare(self.c,self.news,T,True)
        d=self.baseline/'runs'/str(T);d.mkdir(parents=True)
        (d/'manifest.json').write_text(canonical(dict(cutoff=T,plans=plans,plan_hash=digest(plans))))
        self.tech=self.path/'technical.sqlite3'
        self.rows=[[t,99+i,101+i,100+i,100+i,10] for i,t in enumerate(range(T-60300,T-300,300))]
        for symbol in SYMBOLS:technical.save(self.tech,symbol,self.rows,T-100)
        self.c['schema']='phase8b.v1'
        config=self.path/'config.json';config.write_text(canonical(self.c))
        return dict(enabled=True,allow_unpriced_kairos=True,interval_seconds=3600,start_cutoff=T,
                    known_daily_budget_usd=25,known_30day_budget_usd=750,max_daily_calls=480,max_30day_calls=14400,
                    config=str(config),news_db=str(self.news),btc_candles_db=str(self.candles),
                    directory=str(self.path/'b'),additional_batches=[],baseline_root=str(self.baseline),technical_db=str(self.tech))

    def test_indicators_and_point_in_time_first_version(self):
        self.setup_pair()
        state,snap=technical.freeze(self.tech,'BTC-USD',T)
        self.assertEqual(len(snap['candles']),200)
        self.assertEqual(state['rsi14'],100)
        self.assertEqual(state['sma20_usd'],289.5)
        self.assertEqual(state['sma50_usd'],274.5)
        self.assertAlmostEqual(state['macd_usd'],7)
        self.assertAlmostEqual(state['macd_signal_usd'],7)
        self.assertEqual(state['atr14_usd'],2)
        self.assertEqual(state['volume_ratio20'],1)
        changed=copy.deepcopy(self.rows);changed[-1][4]=changed[-1][2]
        technical.save(self.tech,'BTC-USD',changed,T+1)
        self.assertEqual(technical.freeze(self.tech,'BTC-USD',T),(state,snap))
        with technical.connect(self.tech) as c:
            c.execute('UPDATE candles SET observed=? WHERE symbol=? AND start=?',(T+1,'BTC-USD',T-600));c.commit()
        with self.assertRaises(ValueError):technical.freeze(self.tech,'BTC-USD',T)
        with self.assertRaises(ValueError):technical.freeze(self.tech,'BTC-USD',T+1)

    def test_same_news_augmented_all_engines_and_no_replay(self):
        p=self.setup_pair()
        # An article arriving after cutoff cannot change the reused news.
        self.ingest(available=T+1,title='Later article',guid='late',url='https://example.org/late')
        seen=[]
        def call(engine,state,c):
            seen.append((engine,copy.deepcopy(state)))
            return adapters.fixture_invoke(engine,state,c)
        with patch('phase8b.adapters.preflight'):
            r=scheduled.predict(p,lambda:T+30,call)
            self.assertEqual(r['ok_calls'],20)
            again=Mock();self.assertEqual(scheduled.predict(p,lambda:T+31,again)['status'],'already_attempted_no_retry');again.assert_not_called()
        for symbol in SYMBOLS:
            inputs=[s for e,s in seen if s['symbol']==symbol]
            self.assertEqual(len(inputs),4);self.assertTrue(all(s==inputs[0] for s in inputs))
            a=json.loads((self.baseline/'runs'/str(T)/'manifest.json').read_text())['plans'][symbol]['frozen']
            self.assertEqual(inputs[0]['news'],a['state']['news'])
            self.assertEqual(inputs[0]['news_set_id'],a['state']['news_set_id'])
            for engine in adapters.KEYS:
                request=canonical(adapters.request(engine,inputs[0],self.c))
                self.assertIn('technical indicators',request)
                self.assertIn('rsi14',request)
            with store.connect(Path(p['directory'])/'runs'/str(T)/(symbol+'.db'),True) as c:
                result=json.loads(c.execute('SELECT result_json FROM calls LIMIT 1').fetchone()[0])
                self.assertEqual(result['experiment'],'phase8b');self.assertTrue(result['technical_snapshot_id'])
        self.assertNotEqual(store.APP_ID,0x50384131)

    def test_missing_late_or_mismatched_data_skips_no_paid_calls(self):
        p=self.setup_pair();call=Mock()
        with technical.connect(self.tech) as c:c.execute('DELETE FROM candles WHERE start=?',(T-600,));c.commit()
        with patch('phase8b.adapters.preflight'):
            self.assertEqual(scheduled.predict(p,lambda:T+30,call)['status'],'preflight_skipped')
        call.assert_not_called()
        technical.save(self.tech,'BTC-USD',self.rows,T+1)
        self.assertEqual(scheduled.predict(p,lambda:T+31,call)['status'],'already_attempted_no_retry')
        p['directory']=str(self.path/'new')
        self.c['trade_threshold']=.7;Path(p['config']).write_text(canonical(self.c))
        with patch('phase8b.adapters.preflight'):
            self.assertEqual(scheduled.predict(p,lambda:T+30,call)['status'],'preflight_skipped')
        call.assert_not_called()

    def test_four_hour_evaluation_no_provider_and_disabled_forecasts(self):
        p=self.setup_pair()
        with patch('phase8b.adapters.preflight'):scheduled.predict(p,lambda:T+30,adapters.fixture_invoke)
        batch=Path(p['directory'])/'runs'/str(T)
        with market.connect(batch/'asset-prices.sqlite3') as c:
            for symbol in SYMBOLS[1:]:
                for ts,price in ((T-300,100),(T+300,101),(T+14100,102)):
                    c.execute('INSERT INTO prices VALUES(?,?,?,?,?)',(symbol,ts,canonical([ts,price-1,price+1,price,price,1]),ts+360,market.source(symbol)))
        p['enabled']=False
        with patch('phase8b.adapters.invoke',side_effect=AssertionError('No paid calls')),patch('phase8b.market.fetch'):
            self.assertEqual(scheduled.evaluate_batches(p,lambda:T+14459)['newly_evaluated'],0)
            r=scheduled.evaluate_batches(p,lambda:T+14461)
            self.assertEqual(r['newly_evaluated'],5);self.assertEqual(len(r['answers']),20)
            self.assertEqual(r['assets']['BTC-USD']['engines']['gpt6_astra']['paired_nonoverlap_primary']['n'],1)
            self.assertEqual(scheduled.evaluate_batches(p,lambda:T+15000)['newly_evaluated'],0)

    def test_budget_and_no_backfill(self):
        p=self.setup_pair();p['max_daily_calls']=19
        with patch('phase8b.adapters.preflight'):
            with self.assertRaises(ValueError):scheduled.predict(p,lambda:T+30,Mock())
        call=Mock();self.assertEqual(scheduled.predict(p,lambda:T+61,call)['status'],'outside_start_window');call.assert_not_called()

for name in list(vars(Phase8aTests)):
    if name.startswith('test_') and name not in vars(Phase8bTests):setattr(Phase8bTests,name,None)
del Phase8aTests
