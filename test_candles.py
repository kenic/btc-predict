import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import candle_store as store
import collect_candles as collector
import phase6_views as views
from volatility import realized_volatility, classify


def candle(t, close=100):
    return [t, 90, max(120,close), 100, close, 1]


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/'candles.db'

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        with sqlite3.connect(self.db) as c:
            return c.execute('SELECT * FROM candles_5m ORDER BY start_ts').fetchall()

    def test_future_floor_and_safety_delay(self):
        fetch = Mock(return_value=[candle(300),candle(600)])
        self.assertEqual(collector.collect(self.db,fetch,clock=301)['inserted'],0)
        fetch.assert_not_called()
        # first eligible start is 600; 300 was already in progress at startup.
        collector.collect(self.db,fetch,clock=959)
        fetch.assert_not_called()
        result = collector.collect(self.db,fetch,clock=960)
        fetch.assert_called_once_with(600,900)
        self.assertEqual(result['inserted'],1)
        self.assertEqual([r[0] for r in self.rows()],[600])

    def test_completed_only_and_malformed_rejected(self):
        collector.collect(self.db,Mock(),clock=0)
        bad = [candle(300), candle(1), candle(-300), [0,90,99,100,100,1],
               [0,101,120,100,100,1], [0,90,120,100,float('nan'),1],
               [0,90,120,100,0,1], [0,90,120,100,100,-1],
               [0,90,120,100,100,float('inf')], [0,90,120,100,100],
               [0,90,120,100,'100',1], [True,90,120,100,100,1], None]
        # Only the null-volume row is valid; invalid duplicates poison its timestamp.
        result = collector.collect(self.db,lambda a,b:bad,clock=360)
        self.assertEqual(result['inserted'],0)
        self.assertEqual(self.rows(),[])
        with self.assertRaisesRegex(ValueError,'incomplete'):
            store.validate(candle(300),0,600,599)
        result = collector.collect(self.db,lambda a,b:[candle(0),[300,90,120,100,100]],clock=660)
        self.assertEqual(result['inserted'],2)
        self.assertIsNone(self.rows()[1][5])

    def test_idempotent_first_observation_and_conflict(self):
        store.initialize(self.db,0)
        fetch = lambda a,b:[candle(0)]
        self.assertEqual(collector.collect(self.db,fetch,360)['inserted'],1)
        before = self.rows()
        self.assertEqual(collector.collect(self.db,fetch,400)['duplicates'],1)
        self.assertEqual(collector.collect(self.db,lambda a,b:[candle(0,101)],400)['conflicts'],1)
        self.assertEqual(self.rows(),before)
        self.assertEqual(collector.collect(self.db,lambda a,b:[candle(300),candle(300,101)],660)['inserted'],0)

    def test_short_gap_recovery_never_long_backfill(self):
        store.initialize(self.db,0)
        collector.collect(self.db,lambda a,b:[candle(0)],360)
        fetch = Mock(side_effect=lambda a,b:[candle(t) for t in range(a,b,300)])
        collector.collect(self.db,fetch,1260)
        fetch.assert_called_with(0,1200)
        self.assertEqual([r[0] for r in self.rows()],list(range(0,1200,300)))
        collector.collect(self.db,fetch,86460)
        fetch.assert_called_with(84600,86400)
        self.assertEqual(len(self.rows()),10)
        self.assertNotIn(1200,[r[0] for r in self.rows()])
        # Failed collection must preserve the startup floor, not reset it on retry.
        with self.assertRaises(RuntimeError):
            collector.collect(self.db,Mock(side_effect=RuntimeError('network down')),86760)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT first_start_ts FROM collector_state').fetchone()[0],0)

    def test_invalid_response_and_other_db_untouched(self):
        experiment = self.db.parent/'btc.db'
        with sqlite3.connect(experiment) as c:
            c.execute('CREATE TABLE state(value TEXT)')
            c.execute("INSERT INTO state VALUES('frozen')")
        before = experiment.read_bytes()
        collector.collect(self.db,Mock(),0)
        for response in ({'error':'failure'},None):
            with self.assertRaises(ValueError):
                collector.collect(self.db,lambda a,b:response,360)
        with self.assertRaises(RuntimeError):
            collector.collect(self.db,Mock(side_effect=RuntimeError('provider failure')),360)
        self.assertEqual(experiment.read_bytes(),before)

    def test_existing_provider_utility_reused(self):
        store.initialize(self.db,0)
        with patch('prepare_phase4.fetch_candles',return_value=[candle(0)]) as fetch:
            collector.collect(self.db,clock=360)
        fetch.assert_called_once_with(0,300)


class ArchiveEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.db = self.directory/'candles.db'
        self.target = 3600
        store.initialize(self.db,0)
        self.candles = [candle(t,100*1.001**i) for i,t in enumerate(range(3300,7200,300))]
        store.save(self.db,self.candles,7260)
        self.cfg = {'quiet_upper':.2,'active_lower':.4}
        self.connection = sqlite3.connect(':memory:')

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def evaluate(self, clock=7200):
        events = [dict(target=self.target,gate='rejected',single_class='QUIET')]
        return views.actual_volatility(events,self.connection,set(),self.cfg,clock,self.directory)[self.target]

    def test_13_closes_canonical_regime_no_fetch_and_read_only(self):
        before = self.db.read_bytes()
        expected = realized_volatility(self.candles,self.target)
        with patch('prepare_phase4.fetch_candles',side_effect=AssertionError('No web fetch')), \
             patch('direction_volatility.realized_volatility',wraps=realized_volatility) as rv, \
             patch.object(views,'load_candles',side_effect=AssertionError('Dedicated DB has priority')):
            row = self.evaluate()
            rv.assert_called_once()
            self.assertEqual(row['actual_volatility'],expected)
            self.assertEqual(row['actual_regime'],classify(expected,self.cfg))
            for cutoff,regime in ((expected+1,'QUIET'),(expected,'NORMAL')):
                self.cfg=dict(quiet_upper=cutoff,active_lower=expected+2)
                self.assertEqual(self.evaluate()['actual_regime'],regime)
            self.cfg=dict(quiet_upper=0,active_lower=expected)
            self.assertEqual(self.evaluate()['actual_regime'],'ACTIVE')
        self.assertEqual(before,self.db.read_bytes())

    def test_missing_invalid_and_unavailable_store(self):
        with sqlite3.connect(self.db) as c:
            c.execute('DELETE FROM candles_5m WHERE start_ts=5100')
        self.assertEqual(self.evaluate()['actual_volatility_status'],'unavailable')
        store.save(self.db,[candle(5100)],7260)
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE candles_5m SET source='other' WHERE start_ts=5100")
        self.assertEqual(self.evaluate()['actual_volatility_status'],'unavailable')
        # Corrupt or missing dedicated DB must not break web reporting/create files.
        self.db.write_bytes(b'not sqlite')
        self.assertEqual(self.evaluate()['actual_volatility_status'],'unavailable')
        self.db.unlink()
        self.assertEqual(self.evaluate()['actual_volatility_status'],'unavailable')
        self.assertFalse(self.db.exists())

    def test_open_hour_never_reads_archive(self):
        with patch.object(views,'read_window',side_effect=AssertionError('Open hour')):
            self.assertEqual(self.evaluate(7199)['actual_volatility_status'],'pending')

    def test_saved_evaluated_rv_priority_and_unconfirmed_value_ignored(self):
        self.connection.execute('CREATE TABLE phase5_predictions(target_candle_time INTEGER, actual_rv REAL,evaluated_at TEXT)')
        self.connection.execute('INSERT INTO phase5_predictions VALUES(3600,.5,NULL)')
        events = [dict(target=self.target)]
        row = views.actual_volatility(events,self.connection,{'phase5_predictions'},self.cfg,7200,self.directory)[self.target]
        self.assertEqual(row['actual_volatility'],realized_volatility(self.candles,self.target))
        self.connection.execute("UPDATE phase5_predictions SET evaluated_at='evaluated'")
        with patch.object(views,'read_window',side_effect=AssertionError('Saved evaluated RV wins')):
            row = views.actual_volatility(events,self.connection,{'phase5_predictions'},self.cfg,7200,self.directory)[self.target]
        self.assertEqual(row['actual_volatility'],.5)
        self.assertEqual(row['actual_regime'],'ACTIVE')
