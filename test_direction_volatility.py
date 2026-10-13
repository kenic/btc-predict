import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app
import db
import direction_volatility as dv
from volatility import classify


def candles(start=3600):
    return {t:[t,0,0,0,100*math.exp(i*.001),0]
            for i,t in enumerate(range(start-300,start+3600,300))}


class DirectionVolatilityTests(unittest.TestCase):
    def test_exact_window_missing_and_boundaries(self):
        config=dict(quiet_upper=.2,active_lower=.4)
        rows=[dict(target_candle_time=3600),dict(target_candle_time=7200)]
        assigned,missing=dv.assign_rv(rows,candles(),config)
        self.assertEqual(missing,1)
        self.assertAlmostEqual(assigned[0]['rv'],100*math.sqrt(12*.001**2))
        self.assertEqual(assigned[0]['regime'],'NORMAL')
        for x,label in [(0,'QUIET'),(.2,'NORMAL'),(.4,'ACTIVE')]:
            self.assertEqual(classify(x,config),label)
        c=candles();del c[3300]
        self.assertEqual(dv.assign_rv(rows,c,config),( [],2))
        c=candles();c[3600][4]=0
        self.assertEqual(dv.assign_rv(rows,c,config),( [],2))

    def test_grouping_route_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'btc.db'
            with patch.object(db,'DB_PATH',path):
                db.init_db()
            with sqlite3.connect(path) as c:
                for i,(phase,model,t,correct) in enumerate([
                    ('phase1','openai',3600,1),('phase2','jev',3600,0),
                    ('phase3','openai',7200,1)]):
                    c.execute('''INSERT INTO predictions(created_at,candle_time,candle_close,model,p_up,p_down,context,
                      target_candle_time,actual_direction,correct,evaluated_at,actual_return,predictor,phase)
                      VALUES ('fixture',?,100,?,.8,.2,'{}',?,'UP',?,'done',-1.5,?,?)''',
                      (i,model,t,correct,model,phase))
                before='\n'.join(c.iterdump())
            with patch.object(dv,'load_candles',return_value=candles()),patch.object(app,'DB_PATH',path):
                result=dv.report(path)
                self.assertEqual(result['missing'],1)
                stats={(p,m,r):s for p,m,r,s in result['groups']}
                self.assertEqual(stats['all','openai','ALL']['n'],1)
                self.assertEqual(stats['all','jev','ALL']['correct'],0)
                self.assertAlmostEqual(stats['all','openai','ALL']['brier'],.04)
                self.assertEqual(stats['all','openai','ALL']['median_return'],1.5)
                self.assertEqual(dv.report(path,'phase2')['missing'],0)
                for phase in ('all','phase1','phase2','phase3'):
                    response=app.app.test_client().get('/analyze/?phase='+phase)
                    self.assertEqual(response.status_code,200)
                    self.assertIn('Direction accuracy by realized volatility',response.text)
                    self.assertIn('not tradable prospectively',response.text)
                with sqlite3.connect(path) as c:
                    self.assertEqual(before,'\n'.join(c.iterdump()))
