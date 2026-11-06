import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from phase7_views import report

class VolatilityViewTests(unittest.TestCase):
    def test_predictions_actuals_pending_missing_and_readonly(self):
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'phase7.db';candles=Path(d)/'candles.db'
            cfg={'candles':str(candles),'phase6_design':{'gate':{'volatility_config':{'quiet_upper':.2,'active_lower':.4}}}}
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE config(arm_json TEXT)');c.execute('INSERT INTO config VALUES(?)',(json.dumps(cfg),))
                c.execute('CREATE TABLE calls(target INTEGER,kind TEXT,slot INTEGER,status TEXT,payload_json TEXT)')
                c.executemany('INSERT INTO calls VALUES(?,\'class\',0,?,?)',[(3600,'complete',json.dumps([[0,0,1],'fake','{}'])),(7200,'complete',json.dumps([[1,0,0],'fake','{}'])),(10800,'late',json.dumps([[0,0,1],'fake','{}']))])
            with sqlite3.connect(candles) as c:
                c.execute('CREATE TABLE candles_5m(start_ts INTEGER,close REAL)')
                c.executemany('INSERT INTO candles_5m VALUES(?,?)',[(t,100) for t in range(3300,6901,300)])
            base={'recent':[{'target':3600,'gate':'rejected'},{'target':7200,'gate':'accepted'},{'target':10800,'gate':'unknown'}]}
            before={p:p.read_bytes() for p in (db,candles)}
            with patch('phase7_views.frozen_report',side_effect=lambda *a,**kw:copy.deepcopy(base)):
                out=report(db,clock=11000)
            a,b,c=out['recent']
            self.assertEqual(a['volatility_prediction'],'ACTIVE');self.assertEqual(a['actual_volatility'],'QUIET');self.assertEqual(a['actual_rv'],0)
            self.assertEqual(b['volatility_prediction'],'QUIET');self.assertEqual(b['actual_volatility_status'],'unavailable')
            self.assertIsNone(c['volatility_prediction']);self.assertEqual(c['actual_volatility_status'],'pending')
            for p in before:self.assertEqual(before[p],p.read_bytes())
            candles.unlink()
            with patch('phase7_views.frozen_report',side_effect=lambda *a,**kw:copy.deepcopy(base)):
                self.assertEqual(report(db,clock=11000)['recent'][0]['actual_volatility_status'],'unavailable')
            self.assertFalse(candles.exists())
    def test_no_events_never_opens_sources(self):
        with patch('phase7_views.frozen_report',return_value={'recent':[]}),patch('phase7_views.sqlite3.connect',side_effect=AssertionError('No database access expected')):
            self.assertEqual(report('/absent'),{'recent':[]})
    def test_scheduled_render_has_new_headings(self):
        from phase7_views import render
        data=dict(status='Scheduled',start_target=3600,end_exclusive=7200,recorded_hours=0,accepted=0,executable_n=0,missing_hours=0,training={'selector':{},'logistic_n':757},methods={'gpt':{'n':0,'trades':0,'mean_net_bps':None,'max_drawdown_bps':0}},primary={'verdict':'Not tested'},recent=[])
        with patch('phase7_views.report',return_value=data):
            page=render('/absent')
        for title in ('Volatility forecast','Actual volatility','Actual RV (%)'):
            self.assertIn(title,page)
if __name__=='__main__':unittest.main()
