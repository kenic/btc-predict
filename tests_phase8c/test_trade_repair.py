"""The gap check remains strict; repairs only improve future point-in-time input."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from phase8c import micro
from phase8c.core import canonical

T=1794538800

class TradeRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'micro.db'
        with micro.connect(self.path):pass
    def tearDown(self):self.tmp.cleanup()
    def save(self,tid,event=T-10,observed=T-5,symbol='BTC-USD',size=1):
        v=dict(id=tid,event=event,price=100,size=size,aggressor='buy')
        with micro.connect(self.path) as c,c:c.execute('INSERT INTO trades VALUES(?,?,?,?,?)',(symbol,tid,event,observed,canonical(v)))
    def row(self,tid,event=T-10,size=1):return dict(trade_id=tid,time=event,price='100',size=str(size),side='sell')
    def ids(self,cutoff):
        with micro.connect(self.path,True) as c:return [r[0] for r in c.execute('SELECT id FROM trades WHERE observed<=? ORDER BY id',(cutoff,))]

    def test_hole_inside_already_overlapped_page_gets_repaired(self):
        self.save(100);self.save(102)
        with patch('phase8c.micro.get',return_value=([self.row(101),self.row(100)],None)) as get:
            out=micro.repair_recent(self.path,'BTC-USD',lambda:T-1)
        self.assertEqual(out,dict(pages=1,inserted=1));self.assertEqual(self.ids(T),[100,101,102])
        self.assertEqual(get.call_args.kwargs,dict(after='102',limit=1000))
        with micro.connect(self.path,True) as c:
            self.assertEqual(c.execute('SELECT observed FROM trades WHERE id=100').fetchone()[0],T-5)
            self.assertEqual(c.execute('SELECT observed FROM trades WHERE id=101').fetchone()[0],T-1)

    def test_late_repairs_cannot_change_old_cutoff_and_no_overwrite(self):
        self.save(100);self.save(102)
        with patch('phase8c.micro.get',return_value=([self.row(101),self.row(100,size=99)],None)):
            micro.repair_recent(self.path,'BTC-USD',lambda:T+1)
        self.assertEqual(self.ids(T),[100,102]);self.assertEqual(self.ids(T+1),[100,101,102])
        with micro.connect(self.path,True) as c:self.assertEqual(json.loads(c.execute('SELECT raw FROM trades WHERE id=100').fetchone()[0])['size'],1)

    def test_page_budget_and_eventually_available_rows(self):
        for tid in (100,2000,4000):self.save(tid)
        with patch('phase8c.micro.get',return_value=([self.row(101)],None)) as get:
            micro.repair_recent(self.path,'BTC-USD',lambda:T)
        self.assertLessEqual(get.call_count,2);self.assertTrue(micro.recent_gaps(self.path,'BTC-USD',T))

    def test_network_error_empty_future_or_wrong_range_never_inserts(self):
        self.save(100);self.save(102)
        cases=[OSError('secret-not-logged'),([],None),([self.row(101,event=T+10)],None),([self.row(102)],None)]
        for case in cases:
            kw={'side_effect':case} if isinstance(case,Exception) else {'return_value':case}
            with patch('phase8c.micro.get',**kw):micro.repair_recent(self.path,'BTC-USD',lambda:T)
            self.assertEqual(self.ids(T),[100,102])

    def test_older_gaps_and_other_assets_do_not_trigger_requests(self):
        self.save(100,event=T-8000,observed=T-7900);self.save(102,event=T-7990,observed=T-7900)
        with patch('phase8c.micro.get') as get:
            micro.repair_recent(self.path,'BTC-USD',lambda:T)
            micro.repair_recent(self.path,'ETH-USD',lambda:T)
        get.assert_not_called()

    def test_future_observations_do_not_steer_repair(self):
        self.save(100);self.save(102,observed=T+20)
        self.assertEqual(micro.recent_gaps(self.path,'BTC-USD',T),[])

    def test_boundary_predecessor_exposes_recent_start_gap(self):
        self.save(100,event=T-4000,observed=T-3990);self.save(103,event=T-100,observed=T-90)
        self.assertEqual(micro.recent_gaps(self.path,'BTC-USD',T),[(101,102,103)])

    def test_prediction_still_rejects_missing_or_late_trade(self):
        market=Path(self.tmp.name)/'market.db'
        with sqlite3.connect(market) as c:
            c.execute('CREATE TABLE orderbook_samples(timestamp REAL,created_at REAL,best_bid REAL,best_ask REAL,spread REAL,top5_imbalance REAL,top10_imbalance REAL,top20_imbalance REAL,microprice_offset REAL)')
            for t in range(T-3645,T,15):c.execute('INSERT INTO orderbook_samples VALUES(?,?,?,?,?,?,?,?,?)',(t,t,100,101,1,.6,.6,.6,.1))
            c.execute('CREATE TABLE trades(trade_id INTEGER,timestamp REAL,size REAL,aggressor_side TEXT,collected_at REAL)')
        for i,t in enumerate(range(T-3599,T,15),1):
            if i!=100:self.save(i,event=t,observed=t+1)
        with self.assertRaisesRegex(ValueError,'btc_trade_coverage_gap'):micro.freeze(self.path,'BTC-USD',T,str(market))
        event=T-3599+99*15
        with patch('phase8c.micro.get',return_value=([self.row(100,event=event)],None)):
            micro.repair_recent(self.path,'BTC-USD',lambda:T+1)
        with self.assertRaisesRegex(ValueError,'btc_trade_coverage_gap'):micro.freeze(self.path,'BTC-USD',T,str(market))
        # An as-of just after receipt may use it; the original cutoff remains invalid.
        state,_=micro.freeze(self.path,'BTC-USD',T+2,str(market))
        # Advancing the cutoff by 2s excludes the earliest original trade.
        self.assertEqual(state['trade_flow']['60']['trades'],239)

if __name__=='__main__':unittest.main()
