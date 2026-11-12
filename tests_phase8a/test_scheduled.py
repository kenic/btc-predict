import copy
import json
from pathlib import Path
from unittest.mock import patch, Mock
from tests_phase8a.test_phase8a import Phase8aTests, T
from phase8a import scheduled, adapters, pilot, market
from phase8a.core import canonical, SYMBOLS


class ScheduledTests(Phase8aTests):
    def policy(self):
        self.live_config()
        p=self.path/'config.json';p.write_text(canonical(self.c))
        return dict(enabled=True,allow_unpriced_kairos=True,interval_seconds=3600,start_cutoff=T,
                    known_daily_budget_usd=25,known_30day_budget_usd=750,max_daily_calls=480,max_30day_calls=14400,
                    config=str(p),news_db=str(self.news),btc_candles_db=str(self.candles),
                    directory=str(self.path/'scheduled'),additional_batches=[])

    def test_schedule_windows_disabled_no_calls(self):
        p=self.policy();self.ingest()
        call=Mock()
        for now in (T-1,T+61,T+360):
            self.assertEqual(scheduled.predict(p,lambda:now,call)['status'],'outside_start_window')
        p['enabled']=False
        self.assertEqual(scheduled.predict(p,lambda:T+10,call)['status'],'disabled')
        call.assert_not_called()

    def test_hourly_same_cutoff_never_retried_and_contract_frozen(self):
        p=self.policy();self.ingest()
        with patch('phase8a.adapters.preflight'):
            r=scheduled.predict(p,lambda:T+10,adapters.fixture_invoke)
            self.assertEqual(r['ok_calls'],20)
            call=Mock()
            self.assertEqual(scheduled.predict(p,lambda:T+11,call)['status'],'already_attempted_no_retry')
            call.assert_not_called()
            with patch('phase8a.scheduled.source_manifest',return_value={'changed':'yes'}):
                with self.assertRaises(ValueError):scheduled.predict(p,lambda:T+3601,call)

    def test_global_budget_and_failure_reservation(self):
        p=self.policy();path=self.path/'ledger.sqlite3'
        p.update(known_daily_budget_usd=1,known_30day_budget_usd=2,max_daily_calls=20,max_30day_calls=40)
        self.assertTrue(scheduled.reserve(path,T,T+10,.8,p,self.path/'one'))
        self.assertFalse(scheduled.reserve(path,T,T+11,.8,p,self.path/'one'))
        with self.assertRaises(ValueError):scheduled.reserve(path,T+3600,T+3610,.8,p,self.path/'two')
        self.assertTrue(scheduled.reserve(path,T+86400,T+86410,.8,p,self.path/'three'))
        with self.assertRaises(ValueError):scheduled.reserve(path,T+172800,T+172810,.8,p,self.path/'four')
        self.assertTrue(scheduled.reserve(path,T+31*86400,T+31*86400+11,.8,p,self.path/'later'))

    def test_empty_news_skips_without_retry_or_charge(self):
        p=self.policy();call=Mock()
        self.assertEqual(scheduled.predict(p,lambda:T+10,call)['status'],'preflight_skipped')
        self.ingest()
        self.assertEqual(scheduled.predict(p,lambda:T+11,call)['status'],'already_attempted_no_retry')
        call.assert_not_called()

    def test_answers_continue_when_disabled_and_never_call_models(self):
        p=self.policy();self.ingest();self.candles_for()
        with patch('phase8a.adapters.preflight'):
            scheduled.predict(p,lambda:T+10,adapters.fixture_invoke)
        p['enabled']=False;p['known_30day_budget_usd']=.00001
        root=Path(p['directory']);batch=root/'runs'/str(T)
        with market.connect(batch/'asset-prices.sqlite3') as c:
            for symbol in SYMBOLS[1:]:
                for ts,price in ((T-300,100),(T+300,101),(T+14100,102)):
                    c.execute('INSERT INTO prices VALUES(?,?,?,?,?)',(symbol,ts,canonical([ts,price-1,price+1,price,price,1]),ts+360,market.source(symbol)))
        with patch('phase8a.adapters.invoke',side_effect=AssertionError('No paid APIs')),patch('phase8a.market.fetch') as fetch:
            early=scheduled.evaluate_batches(p,lambda:T+14459)
            self.assertEqual(early['newly_evaluated'],0);fetch.assert_not_called()
            r=scheduled.evaluate_batches(p,lambda:T+14461)
            self.assertEqual(r['newly_evaluated'],5);self.assertEqual(len(r['answers']),20)
            self.assertTrue(all(a['actual_up']==1 for a in r['answers']))
            for a in r['assets'].values():
                self.assertEqual(a['engines']['gpt6_astra']['paired_nonoverlap_primary']['n'],1)
                self.assertAlmostEqual(a['engines']['gpt6_astra']['paired_hourly_descriptive']['brier'],.16)
            again=scheduled.evaluate_batches(p,lambda:T+15000)
            self.assertEqual(again['newly_evaluated'],0)

    def test_price_failure_does_not_block_other_assets(self):
        p=self.policy();self.ingest();self.candles_for()
        with patch('phase8a.adapters.preflight'):scheduled.predict(p,lambda:T+10,adapters.fixture_invoke)
        with patch('phase8a.market.fetch',side_effect=TimeoutError('private error')):
            r=scheduled.evaluate_batches(p,lambda:T+15000)
        self.assertEqual(r['newly_evaluated'],1)
        self.assertEqual(len(r['errors']),4)
        self.assertNotIn('private error',canonical(r))

for name in list(vars(Phase8aTests)):
    if name.startswith('test_') and name not in vars(ScheduledTests):setattr(ScheduledTests,name,None)
del Phase8aTests
