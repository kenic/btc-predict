import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import phase5b as b
from phase5b_analysis import JOIN_SQL, summarize
from volatility import realized_volatility


class Phase5bTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name)/'btc.db'
        self.cfg = patch.object(b, 'specification', return_value={'version': b.VERSION})
        self.cfg.start()
        self.addCleanup(self.cfg.stop)
        self.state = b.initialize(self.db, clock=3600)
        self.start = self.state['start_target']

    def rows(self):
        with b.database(self.db) as c:
            return [dict(r) for r in c.execute('SELECT * FROM phase5b_predictions ORDER BY target_candle_time')]

    def run_slot(self, target=None, call=None, builder=None, delay=90):
        target = self.start if target is None else target
        b.run(self.db, clock_fn=lambda: target+delay,
              builder=builder or (lambda t: ('snapshot '+str(t), .25)),
              call=call or (lambda s: (.3, 'test-model', '{"rv":0.3}')))

    def test_future_init_idempotent_and_no_existing_changes(self):
        with b.database(self.db) as c:
            c.execute('CREATE TABLE phase6_config (frozen TEXT)')
            c.execute("INSERT INTO phase6_config VALUES ('unchanged')")
        b.migrate(self.db)
        self.assertEqual(b.initialize(self.db, clock=900000), self.state)
        with b.database(self.db) as c:
            self.assertEqual(c.execute('SELECT frozen FROM phase6_config').fetchone()[0], 'unchanged')
        self.assertEqual(self.state['end_target']-self.start, 499*3600)

    def test_all_targets_without_gate_and_no_duplicate_calls(self):
        calls = []
        call = lambda s: (calls.append(s) or .3, 'model', '{}')
        self.run_slot(call=call)
        self.run_slot(call=call)
        self.run_slot(self.start+3600, call=call)
        self.assertEqual(len(calls), 2)
        self.assertEqual([r['status'] for r in self.rows()], ['complete', 'complete'])
        self.assertEqual(self.rows()[0]['snapshot_hash'], b.digest('snapshot '+str(self.start)))

    def test_failures_count_and_next_hour_continues(self):
        def fail(s):
            raise RuntimeError('provider unavailable')
        self.run_slot(call=fail)
        self.run_slot(call=fail)
        self.run_slot(self.start+3600)
        self.assertEqual([r['status'] for r in self.rows()], ['failed', 'complete'])

    def test_missing_snapshot_saved_no_api(self):
        def missing(t):
            raise ValueError('Missing 5m closes')
        with patch('next_stages.regression_call') as call:
            self.run_slot(builder=missing, call=call)
            call.assert_not_called()
        self.assertIsNone(self.rows()[0]['previous_rv'])
        self.assertEqual(self.rows()[0]['status'], 'failed')

    def test_fixed_500_slots_including_missed_no_replacement(self):
        self.run_slot()
        self.run_slot(self.start+500*3600)
        rows = self.rows()
        self.assertEqual(len(rows), 500)
        self.assertEqual(sum(r['status'] == 'missed' for r in rows), 499)
        self.assertEqual(rows[-1]['target_candle_time'], self.state['end_target'])

    def test_late_scheduling_no_call(self):
        with patch('next_stages.regression_call') as call:
            self.run_slot(call=call, delay=601)
            call.assert_not_called()
        self.assertEqual(self.rows()[0]['status'], 'missed')

    def test_uncertain_started_does_not_retry_or_block_other_targets(self):
        self.run_slot()
        with b.database(self.db) as c:
            c.execute("UPDATE phase5b_predictions SET status='started',predicted_rv=NULL")
        with patch('next_stages.regression_call') as call:
            self.run_slot(call=call)
            call.assert_not_called()
        self.run_slot(self.start+3600)
        self.assertEqual([r['status'] for r in self.rows()], ['started', 'complete'])

    def test_snapshot_timeout_and_late_response(self):
        times = iter([self.start+90, self.start+601])
        with patch('next_stages.regression_call') as call:
            b.run(self.db, clock_fn=lambda: next(times), builder=lambda t: ('s', .1), call=call)
            call.assert_not_called()
        target = self.start+3600
        times = iter([target+90, target+100, target+601])
        b.run(self.db, clock_fn=lambda: next(times), builder=lambda t: ('s', .1), call=lambda s: (.2,'m','{}'))
        self.assertEqual([r['status'] for r in self.rows()], ['failed', 'late'])

    def test_validation_and_freeze(self):
        self.run_slot(call=lambda s: (float('nan'), 'm', '{}'))
        self.assertEqual(self.rows()[0]['status'], 'failed')
        with patch.object(b, 'specification', return_value={'changed': True}):
            with self.assertRaisesRegex(RuntimeError, 'Frozen'):
                self.run_slot(self.start+3600)
        self.assertEqual(len(self.rows()), 1)

    def test_overlapping_processes_claim_target_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        ready, release = Event(), Event()
        calls = []
        def builder(t):
            ready.set()
            if not release.wait(3):
                raise RuntimeError('Test synchronization timeout')
            return 'snapshot', .1
        def call(s):
            calls.append(s)
            return .2, 'm', '{}'
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.run_slot, builder=builder, call=call)
            self.assertTrue(ready.wait(3))
            second = pool.submit(self.run_slot, call=call)
            try:
                second.result(timeout=3)
            finally:
                release.set()
            first.result(timeout=3)
        self.assertEqual(calls, ['snapshot'])
        self.assertEqual(len(self.rows()), 1)

    def test_actual_missing_pending_and_canonical_archive(self):
        from candle_store import initialize, save
        self.run_slot()
        archive = Path(self.tmp.name)/'candles.db'
        b.evaluate(self.db, archive, clock=self.start+4000)
        self.assertFalse(archive.exists())
        self.assertIsNone(self.rows()[0]['evaluated_at'])
        initialize(archive, self.start-600)
        candles = [[t, 90, 120, 100, 100+i, 1]
                   for i, t in enumerate(range(self.start-300, self.start+3600, 300))]
        save(archive, candles[:-1], self.start+4000)
        b.evaluate(self.db, archive, clock=self.start+4000)
        self.assertIsNone(self.rows()[0]['actual_rv'])
        save(archive, candles[-1:], self.start+4000)
        b.evaluate(self.db, archive, clock=self.start+4000)
        self.assertAlmostEqual(self.rows()[0]['actual_rv'], realized_volatility(candles, self.start))
        old = self.rows()[0]['evaluated_at']
        b.evaluate(self.db, archive, clock=self.start+5000)
        self.assertEqual(self.rows()[0]['evaluated_at'], old)

    def test_join_exact_timestamp_and_paired_metrics(self):
        self.run_slot()
        from unittest.mock import patch as mock_patch
        import phase6
        with mock_patch.object(phase6, 'DB_PATH', self.db):
            phase6.migrate()
        with b.database(self.db) as c:
            c.execute("INSERT INTO phase6_events(target,started_at,gate,actual_return) VALUES(?, 'now', 'accepted', 2)", (self.start,))
            for method, correct, pnl in [('gpt',1,2), ('logistic',0,-2)]:
                c.execute('''INSERT INTO phase6_predictions(target,method,status,started_at,version,correct,signed_pnl)
                    VALUES(?,?,'complete','now','v',?,?)''', (self.start,method,correct,pnl))
            rows = [dict(r) for r in c.execute(JOIN_SQL)]
        report = summarize(rows)
        self.assertEqual(report['percentile_population'], 1)
        self.assertEqual(report['bins'][-1]['gpt']['paired']['accuracy'], 1)
        self.assertEqual(report['bins'][-1]['logistic']['paired']['total_pnl'], -2)
        rows[0]['logistic_status'] = 'unavailable'
        self.assertEqual(summarize(rows)['bins'][-1]['paired_N'], 0)
        rows[0]['status'] = 'late'
        self.assertEqual(summarize(rows)['percentile_population'], 0)


if __name__ == '__main__':
    unittest.main()
