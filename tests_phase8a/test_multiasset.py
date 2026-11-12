import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from phase8a import core, adapters, market, pilot, evaluation
from tests_phase8a.test_phase8a import Phase8aTests, T, ROOT


class MultiAssetTests(Phase8aTests):
    # Reuse fixtures, not the inherited test cases (filtered below).
    def test_asset_routing_and_link_ambiguity(self):
        def n(title): return {'title':title, 'summary':''}
        self.assertIsNone(core.relevance(n('Click the link for solar updates'), 'LINK-USD'))
        self.assertEqual(core.relevance(n('LINK staking expands'), 'LINK-USD'), 'direct')
        self.assertEqual(core.relevance(n('Chainlink staking expands'), 'LINK-USD'), 'direct')
        self.assertEqual(core.relevance(n('Crypto market regulation'), 'ETH-USD'), 'market_common')
        self.assertIsNone(core.relevance(n('Solana network upgrade'), 'BTC-USD'))
        for symbol in core.SYMBOLS:
            c = copy.deepcopy(self.c); c['symbol'] = symbol
            core.validate_config(c)
            self.assertNotIn('BTC-USD price', adapters.request('gpt6_astra', {'symbol':symbol}, c)['instructions'])

    def test_direct_priority_unrelated_excluded_and_attribution(self):
        self.c['symbol']='ETH-USD'; self.c['max_news']=1
        self.ingest(title='Inflation release', guid='m', url='https://example.org/m')
        self.ingest(title='Ethereum upgrade', guid='d', url='https://example.org/d', published=T-300)
        self.ingest(title='Gardening update', guid='x', url='https://example.org/x')
        self.run_cutoff()
        with __import__('phase8a.store', fromlist=['connect']).connect(self.db, True) as con:
            f=json.loads(con.execute('SELECT input_json FROM cohorts').fetchone()[0])
        self.assertEqual(f['state']['news'][0]['title'], 'Ethereum upgrade')
        self.assertIn('attribution', f['state']['news'][0])
        self.assertIn('not_relevant', [d['reason'] for d in f['news_set']['decisions']])
        self.assertTrue(all(r['symbol']=='ETH-USD' for r in self.calls().values()))

    def test_cross_asset_price_isolation(self):
        path=self.path/'assets.sqlite3'
        with market.connect(path) as c:
            for symbol, base in [('ETH-USD',100),('SOL-USD',20)]:
                for ts, price in ((T-300,base),(T+300,base+1),(T+14100,base+2)):
                    c.execute('INSERT INTO prices VALUES(?,?,?,?,?)',(symbol,ts,core.canonical([ts,price-1,price+1,price,price,1]),ts+360,market.source(symbol)))
        self.assertEqual(market.prices(path,'SOL-USD',T,14400,T+15000)['reference_price'],20)
        self.assertIsNone(market.prices(path,'XRP-USD',T,14400,T+15000))
        self.assertIsNone(market.prices(path,'ETH-USD',T,14400,T+14459))
        self.candles_for()
        with self.assertRaises(ValueError):
            evaluation.prices(self.candles,T,14400,T+15000,'ETH-USD')
        self.c['symbol']='ETH-USD'; self.ingest(title='Ethereum upgrade'); self.run_cutoff()
        self.assertEqual(evaluation.evaluate(self.db,path,lambda:T+15000)['evaluated'],1)

    def test_pilot_global_budget_no_replay_and_same_snapshot(self):
        self.ingest(); self.live_config()
        self.c['engines']['kairos'].update(pricing_verified=False,input_per_million_usd=None,output_per_million_usd=None,per_call_usd=None)
        out=self.path/'pilot'
        with patch('phase8a.adapters.preflight'):
            with self.assertRaises(ValueError):
                pilot.run_batch(self.c,self.news,out,T,known_budget_usd=.000001,allow_unpriced_kairos=True,clock=lambda:T+10)
            self.assertFalse((out/'manifest.json').exists())
            r=pilot.run_batch(self.c,self.news,out,T,allow_unpriced_kairos=True,clock=lambda:T+10,call=adapters.fixture_invoke)
            self.assertEqual(len(r['assets']),5)
            self.assertEqual(r['unknown_cost_calls'],5)
            self.assertIsNone(r['total_cost_usd'])
            for a in r['assets'].values():
                self.assertEqual(len({v['input_id'] for v in a['results']}),1)
                self.assertTrue(all(v['status']=='ok' for v in a['results']))
            call=Mock()
            with self.assertRaises(ValueError):
                pilot.run_batch(self.c,self.news,out,T,allow_unpriced_kairos=True,clock=lambda:T+10,call=call)
            call.assert_not_called()
            self.assertFalse(self.c['engines']['kairos']['pricing_verified'])

    def test_unknown_kairos_exception_does_not_bypass_other_checks(self):
        self.live_config();self.c['engines']['kairos']['pricing_verified']=False
        env={k:'dummy' for k in core.KEYS.values()}
        with patch.dict(os.environ, env):
            with self.assertRaises(adapters.ProviderError): adapters.preflight(self.c,{})
            self.c['engines']['gpt6_astra']['pricing_verified']=False
            with self.assertRaises(adapters.ProviderError): adapters.preflight(self.c,{},allow_unpriced_kairos=True)
            self.c['engines']['gpt6_astra']['pricing_verified']=True
            self.c['engines']['kairos']['account_verified']=False
            with self.assertRaises(adapters.ProviderError): adapters.preflight(self.c,{},allow_unpriced_kairos=True)

# Keep shared helpers without running the inherited suite twice.
for name in list(vars(Phase8aTests)):
    if name.startswith('test_') and name not in vars(MultiAssetTests):
        setattr(MultiAssetTests, name, None)
del Phase8aTests
