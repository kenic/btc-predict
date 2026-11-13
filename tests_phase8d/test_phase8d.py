import copy,json,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
from tests_phase8a.test_phase8a import Phase8aTests as Fixture,T,ROOT
from phase8d import store,events,runtime,adapters,evaluation
from phase8d.core import canonical,SYMBOLS,ENGINES
from phase8pre2 import market,technical

class EventTests(unittest.TestCase):
    ingest=Fixture.ingest
    def setUp(self):
        Fixture.setUp(self);self.c=json.loads((ROOT/'phase8c.config.example.json').read_text());self.c.update(schema='phase8d.v1',interval_seconds=1,trade_threshold=.55,live_enabled=True)
        self.tech=self.path/'technical.sqlite3';self.quotes=self.path/'quotes.sqlite3';market.initialize(self.quotes)
        self.policy=dict(model_config=self.c,quotes_db=str(self.quotes),max_daily_calls=100,daily_known_budget_usd=100,rolling30_known_budget_usd=3000,fee_bps_per_side=5,slippage_bps_per_side=5)
        store.initialize(self.db,self.policy,T-30);events.bootstrap(self.db,self.news)
        for symbol in SYMBOLS:
            for seconds in technical.FRAMES:
                latest=T//seconds*seconds-2*seconds
                rows=[[t,99+i,101+i,100+i,100+i,10] for i,t in enumerate(range(latest-199*seconds,latest+1,seconds))]
                technical.save(self.tech,symbol,seconds,rows,T-20)
            self.quote(symbol,T-2,T-1,99,101)
    tearDown=Fixture.tearDown
    def quote(self,symbol,started,observed,bid,ask):
        with market.connect(self.quotes) as c,c:c.execute('INSERT INTO quotes(symbol,started,observed,bid,ask,raw_json) VALUES(?,?,?,?,?,?)',(symbol,started,observed,bid,ask,'{}'))
    def discover(self,title='Bitcoin rises'):
        self.ingest(available=T,title=title);events.discover(self.db,self.news,lambda:T+1)
        with store.connect(self.db,True) as c:return [dict(r) for r in c.execute('SELECT * FROM events')]
    def execute(self,eid,provider=adapters.fixture_invoke):
        return runtime.execute(self.db,self.news,self.tech,eid,self.policy,lambda:T+5,provider)
    def test_two_assets_only_and_duplicates_revisions_do_not_trigger(self):
        self.assertEqual(SYMBOLS,('BTC-USD','XRP-USD'));self.assertEqual(len(self.discover()),1)
        self.ingest(available=T+1,title='Bitcoin revision');events.discover(self.db,self.news,lambda:T+2)
        self.ingest(available=T+2,title='Bitcoin rises',guid='b',url='https://example.org/b');events.discover(self.db,self.news,lambda:T+3)
        with store.connect(self.db,True) as c:self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],1)
    def test_common_news_two_events_and_same_four_inputs(self):
        rows=self.discover('Crypto market news');self.assertEqual(len(rows),2)
        seen=[]
        def provider(e,state,c):seen.append(copy.deepcopy(state));return adapters.fixture_invoke(e,state,c)
        self.assertEqual(self.execute(rows[0]['id'],provider),'complete');self.assertEqual(len(seen),4);self.assertTrue(all(s==seen[0] for s in seen));self.assertEqual(seen[0]['cutoff'],T)
        self.assertEqual(self.execute(rows[0]['id'],provider),'already_claimed');self.assertEqual(len(seen),4)
    def test_no_bootstrap_backfill(self):
        self.ingest(available=T-40)
        # Existing rows acquired before activation never trigger even when discovered later.
        events.discover(self.db,self.news,lambda:T)
        with store.connect(self.db,True) as c:self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],0)
    def test_late_event_no_calls(self):
        row=self.discover()[0]
        self.assertEqual(runtime.execute(self.db,self.news,self.tech,row['id'],self.policy,lambda:T+61,adapters.fixture_invoke),'unavailable')
        with store.connect(self.db,True) as c:self.assertEqual(c.execute('SELECT count(*) FROM calls').fetchone()[0],0)
    def test_future_technical_and_quotes_not_used(self):
        row=self.discover()[0];before=events.prepare(self.db,self.news,self.tech,row,self.c)
        self.quote('BTC-USD',T,T+1,200,201)
        changed=events.prepare(self.db,self.news,self.tech,row,self.c);self.assertEqual(before,changed)
        with technical.connect(self.tech) as c,c:c.execute('UPDATE candles SET observed=? WHERE symbol=? AND seconds=300 AND start=(SELECT max(start) FROM candles WHERE symbol=? AND seconds=300)',(T+1,'BTC-USD','BTC-USD'))
        with self.assertRaises(ValueError):technical.freeze(self.tech,'BTC-USD',T)
    def test_fractional_cutoff_uses_latest_known_confirmed_bars(self):
        state,snap=technical.freeze(self.tech,'BTC-USD',T+123.5)
        self.assertEqual(snap['cutoff'],T+123.5);self.assertNotIn('microstructure',state)
        self.assertTrue(all(r['observed_at']<=T+123.5 for rows in snap['candles'].values() for r in rows))
    def test_budget_and_crash_no_retry(self):
        row=self.discover()[0];self.policy['max_daily_calls']=3
        self.assertEqual(self.execute(row['id']),'unavailable')
        with store.connect(self.db) as c,c:c.execute("UPDATE events SET status='dispatched' WHERE id=?",(row['id'],))
        runtime.recover(self.db);self.assertEqual(self.execute(row['id']),'already_claimed')
    def test_provider_failures_preserved_and_not_retried(self):
        row=self.discover()[0]
        def fail(*args):raise adapters.ProviderError('network_or_timeout_unknown_charge')
        self.assertEqual(self.execute(row['id'],fail),'partial');self.assertEqual(self.execute(row['id']),'already_claimed')
        with store.connect(self.db,True) as c:self.assertEqual(c.execute('SELECT count(*) FROM calls').fetchone()[0],4)
    def test_quote_request_must_start_after_completion(self):
        self.quote('BTC-USD',T+4,T+6,102,104);self.quote('BTC-USD',T+6,T+7,104,106)
        with market.connect(self.quotes,True) as c:q=market.after(c,'BTC-USD',T+5)
        self.assertEqual(q['observed'],T+7)
    def test_evaluation_uses_response_entry_and_fixed_target(self):
        row=self.discover()[0];self.execute(row['id'])
        self.quote('BTC-USD',T+6,T+7,104,106);self.quote('BTC-USD',T+14400,T+14401,109,111)
        self.assertEqual(evaluation.evaluate(self.db,self.policy,lambda:T+14419),0)
        self.assertEqual(evaluation.evaluate(self.db,self.policy,lambda:T+14421),1)
        with store.connect(self.db,True) as c:o=json.loads(c.execute('SELECT result_json FROM evaluations').fetchone()[0])
        self.assertEqual(o['reference_price'],100);self.assertEqual(o['price_up'],1)
        executed=[p for p in o['engines'].values() if p['status']=='executed'];self.assertTrue(executed)
        self.assertTrue(all(p['entry_at']==T+7 and p['exit_at']==T+14401 for p in executed));self.assertEqual(evaluation.evaluate(self.db,self.policy,lambda:T+14422),0)
    def test_missing_target_quote_is_not_backfilled(self):
        row=self.discover()[0];self.execute(row['id']);self.quote('BTC-USD',T+14416,T+14417,109,111)
        evaluation.evaluate(self.db,self.policy,lambda:T+14421)
        with store.connect(self.db,True) as c:o=json.loads(c.execute('SELECT result_json FROM evaluations').fetchone()[0])
        self.assertIsNone(o['price_up'])
    def test_thresholds_and_all_probability_scores(self):
        self.assertEqual([evaluation.decision(x) for x in (.45,.45001,.5,.54999,.55)],['DOWN','ABSTAIN','ABSTAIN','ABSTAIN','UP'])
        s=evaluation.scores([(.45,0),(.5,1),(.55,1)]);self.assertEqual(s['n'],3);self.assertEqual(s['judged'],2);self.assertEqual(s['accuracy'],1);self.assertAlmostEqual(s['abstention_rate'],1/3)
    def test_short_spread_costs(self):
        q=dict(id=1,bid=99,ask=101,observed=T)
        p=evaluation.paper({'probability_up':.4},q,q,5,5);self.assertLess(p['net_return'],0)
    def test_validation_excluded_and_nonoverlap_fixed_before_outcomes(self):
        def r(i,t,kind='live',p=.6):
            calls={e:dict(status='ok',probability_up=p,completed_at=t+2) for e in ENGINES}
            return dict(id=str(i),symbol='BTC-USD',kind=kind,cutoff=t,target=t+14400,calls=calls,evaluation=dict(price_up=1,engines={}))
        rows=[r(1,T),r(2,T+60),r(3,T+14400),r(4,T+28800,'validation')]
        a=evaluation.aggregate(rows)['BTC-USD']['gpt6_astra'];self.assertEqual(a['events'],3);self.assertEqual(a['nonoverlap']['n'],2)
    def test_ui_redacts_requests_and_escapes_news(self):
        row=self.discover('Bitcoin &lt;script&gt;evil&lt;/script&gt;')[0];self.execute(row['id'])
        from phase8d_views import report,render
        d=report(self.db,self.quotes);s=render(d)
        self.assertNotIn('request_json',canonical(d));self.assertNotIn('<script>evil',s);self.assertIn('Phase 8d',s)
        with self.assertRaises(ValueError):report(self.db,self.quotes,'ETH-USD')
