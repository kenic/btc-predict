import copy
import io
import json
import math
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import candle_store
from news import store as ns
from news.feeds import load_config, parse_rss
from phase8a import adapters, core, evaluation, runtime, store
from phase8a.cli import main

ROOT = Path(__file__).resolve().parent.parent
T = 1794398400 // 14400 * 14400


class Phase8aTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.db, self.news, self.candles = [self.path/n for n in ('phase8a.db', 'news.db', 'candles.db')]
        self.c = json.loads((ROOT/'phase8a.config.example.json').read_text())
        self.source = load_config(ROOT/'news.config.example.json')['feeds'][1]
        ns.initialize(self.news)

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, available=T-10, published=T-120, title='Economic news', guid='a', url='https://example.org/a', source=None):
        source = source or self.source
        date = datetime.fromtimestamp(published, timezone.utc).isoformat()
        xml = ('<rss><channel><item><guid>'+guid+'</guid><title>'+title+'</title><link>'+url+
               '</link><pubDate>'+date+'</pubDate><description>English summary</description></item></channel></rss>').encode()
        entries, _ = parse_rss(xml, source)
        rid = ns.start_run(self.news, source, 3600, lambda: available-1)
        ns.finish_run(self.news, rid, 'ok', entries=entries, clock=lambda: available)

    def run_cutoff(self, cutoff=T, **kw):
        return runtime.run(self.db, self.news, self.c, cutoff, clock=kw.pop('clock', lambda: cutoff+10), **kw)

    def calls(self):
        with store.connect(self.db, True) as conn:
            return {r['engine']: json.loads(r['result_json']) for r in conn.execute('SELECT * FROM calls')}

    def candles_for(self, t=T, end_price=102):
        if not self.candles.exists():
            candle_store.initialize(self.candles, t-600)
        for ts, price in ((t-300, 100), (t+300, 101), (t+14400-300, end_price)):
            candle_store.save(self.candles, [[ts, price-1, price+1, price, price, 1]], ts+360)

    def live_config(self):
        self.c['live_enabled'] = True
        for e in self.c['engines'].values():
            e.update(account_verified=True, pricing_verified=True, input_per_million_usd=1,
                     output_per_million_usd=1, per_call_usd=0)

    def test_point_in_time_revision_late_arrival(self):
        self.ingest()
        self.ingest(available=T+1, title='Future revision')
        self.ingest(available=T+2, guid='b', url='https://example.org/b', title='Late news')
        frozen = core.freeze(ns.snapshot(self.news, T, deduplicate=False), self.c, T)
        self.assertEqual([n['title'] for n in frozen['state']['news']], ['Economic news'])
        self.assertEqual(frozen['state']['news'][0]['version_id'], 1)
        self.assertEqual(len(frozen['snapshot']['news']), 1)

    def test_latest_future_publication_does_not_fallback(self):
        self.ingest(available=T-20)
        self.ingest(available=T-10, published=T+1, title='Revision')
        self.assertEqual(self.run_cutoff()['reason'], 'no_eligible_news')

    def test_dedup_and_order_are_model_independent(self):
        self.ingest(available=T-20)
        self.ingest(available=T-10, guid='b', url='https://example.org/b')
        snap = ns.snapshot(self.news, T, deduplicate=False)
        a = core.freeze(snap, self.c, T)
        self.assertEqual(len(a['state']['news']), 1)
        self.assertEqual(a['news_set']['decisions'][1]['reason'], 'exact_duplicate')
        snap['news'].reverse()
        b = core.freeze(snap, self.c, T)
        self.assertEqual(a['state']['news'], b['state']['news'])

    def test_empty_news_no_provider_calls(self):
        call = Mock()
        self.assertEqual(self.run_cutoff(call=call)['reason'], 'no_eligible_news')
        call.assert_not_called()
        self.assertTrue(all(r['status'] == 'skipped' for r in self.calls().values()))

    def test_stale_source_prevents_calls(self):
        self.ingest(available=T-7200, published=T-8000)
        call = Mock()
        self.assertEqual(self.run_cutoff(call=call)['reason'], 'no_eligible_news')
        call.assert_not_called()

    def test_unhealthy_source_isolated_and_three_conditions_share_selection(self):
        import copy
        from phase8b import core as bcore
        from phase8c import core as ccore
        self.ingest(available=T-7200,published=T-8000,title='Bitcoin stale source')
        alternate=copy.deepcopy(self.source);alternate['id']='coindesk'
        self.ingest(source=alternate,title='Bitcoin fresh source',guid='healthy',url='https://example.org/healthy')
        snap=ns.snapshot(self.news,T,deduplicate=False)
        selections=[module.freeze(snap,self.c,T) for module in (core,bcore,ccore)]
        self.assertEqual(len({v['state']['news_set_id'] for v in selections}),1)
        self.assertEqual([n['source_id'] for n in selections[0]['state']['news']],['coindesk'])
        self.assertIn('source_unavailable',[d['reason'] for d in selections[0]['news_set']['decisions']])
        seen=[]
        def call(e,state,c):
            seen.append(copy.deepcopy(state));return adapters.fixture_invoke(e,state,c)
        self.run_cutoff(call=call)
        self.assertEqual(len(seen),4);self.assertTrue(all(v==seen[0] for v in seen))
        self.assertTrue(all(r['status']=='ok' for r in self.calls().values()))

    def test_partial_collection_export_keeps_healthy_news(self):
        import copy
        from deploy.export_readonly_archive import export
        self.ingest(available=T-7200,published=T-8000)
        alternate=copy.deepcopy(self.source);alternate['id']='coindesk'
        self.ingest(source=alternate,title='Bitcoin new feed',guid='fresh',url='https://example.org/fresh')
        rid=ns.start_run(self.news,self.source,3600,lambda:T-6)
        ns.finish_run(self.news,rid,'failed',http_status=503,error='http_error',clock=lambda:T-5)
        destination=self.path/'reader-news.db'
        export(self.news,destination,'news')
        selected=core.freeze(ns.snapshot(destination,T,deduplicate=False),self.c,T)['state']['news']
        self.assertEqual([v['source_id'] for v in selected],['coindesk'])

    def test_unknown_source_health_fails_closed(self):
        self.ingest();snap=ns.snapshot(self.news,T,deduplicate=False);snap['coverage']=[]
        self.assertEqual(core.freeze(snap,self.c,T)['state']['news'],[])

    def test_shared_input_concurrent_dispatch_and_no_mutation(self):
        self.ingest()
        seen, barrier = {}, threading.Barrier(4)
        def call(e, state, c):
            seen[e] = core.digest(state)
            barrier.wait(timeout=5)
            state['news'].clear()
            return adapters.fixture_invoke(e, state, c)
        r = self.run_cutoff(call=call)
        self.assertEqual(r['status'], 'complete')
        self.assertEqual(len(set(seen.values())), 1)
        self.assertEqual(len(set(x['input_id'] for x in self.calls().values())), 1)
        self.assertTrue(all(len(x['news_versions']) == 1 for x in self.calls().values()))

    def test_same_cutoff_never_retried(self):
        self.ingest()
        call = Mock(side_effect=TimeoutError('secret exception'))
        self.run_cutoff(call=call)
        self.run_cutoff(call=call)
        self.assertEqual(call.call_count, 4)
        self.assertNotIn('secret exception', core.canonical(self.calls()))

    def test_partial_failure_excluded_from_paired_not_other_engines(self):
        self.ingest()
        def call(e, s, c):
            if e == 'jev':
                raise adapters.ProviderError('http_error', 429)
            return adapters.fixture_invoke(e, s, c)
        self.run_cutoff(call=call)
        self.candles_for()
        evaluation.evaluate(self.db, self.candles, lambda: T+15000)
        report = evaluation.report(self.db)
        self.assertEqual(report['paired_nonoverlap_n'], 0)
        self.assertEqual(report['engines']['gpt6_astra']['all_available_unpaired']['n'], 1)
        self.assertEqual(self.calls()['jev']['http_status'], 429)

    def test_timezone_and_naive_rejection(self):
        self.assertEqual(core.timestamp('2026-11-12T09:00:00+09:00'), core.timestamp('2026-11-12T00:00:00Z'))
        with self.assertRaises(ValueError):
            core.timestamp('2026-11-12T00:00:00')
        for value in (float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                core.timestamp(value)

    def test_live_past_future_and_unaligned_rejected(self):
        for cutoff, now in ((T, T+61), (T, T-1), (T+1, T+10)):
            with self.assertRaises(ValueError):
                self.run_cutoff(cutoff, mode='live', clock=lambda: now)

    def test_offline_live_config_changes_cannot_mix(self):
        self.ingest()
        self.run_cutoff()
        with self.assertRaises(ValueError):
            self.run_cutoff(mode='live')
        changed = copy.deepcopy(self.c)
        changed['horizon_seconds'] = 7200
        with self.assertRaises(ValueError):
            store.initialize(self.db, changed)

    def test_changed_source_code_cannot_resume(self):
        store.initialize(self.db, self.c)
        with patch('phase8a.store.source_manifest', return_value={'changed': 'yes'}):
            with self.assertRaises(ValueError):
                store.initialize(self.db, self.c)

    def test_foreign_database_unchanged(self):
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE important(x)')
        con.close()
        before = self.db.read_bytes()
        with self.assertRaises(ValueError):
            store.initialize(self.db, self.c)
        self.assertEqual(before, self.db.read_bytes())

    def test_budget_is_atomic_and_survives_crash(self):
        self.ingest()
        store.initialize(self.db, self.c)
        frozen = core.freeze(ns.snapshot(self.news, T), self.c, T)
        for t in (T, T+3600):
            store.add_cohort(self.db, t, t+10, frozen, 'live')
        def attempt(t):
            try:
                store.reserve_all(self.db, t, T+10, self.c)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, (T, T+3600))), 1)
        with store.connect(self.db, True) as c:
            self.assertEqual(c.execute('SELECT sum(reserved_usd) FROM calls').fetchone()[0], 1)
        self.assertEqual(self.run_cutoff(mode='live')['status'], 'already_recorded_no_retry')

    def test_daily_budget_and_clock_regression(self):
        self.ingest()
        c = copy.deepcopy(self.c)
        c.update(total_budget_usd=3, max_calls=12)
        store.initialize(self.db, c)
        f = core.freeze(ns.snapshot(self.news, T), c, T)
        for t in (T, T+3600, T+86400):
            store.add_cohort(self.db, t, t+10, f, 'live')
        store.reserve_all(self.db, T, T+10, c)
        with self.assertRaises(ValueError):
            store.reserve_all(self.db, T+3600, T+11, c)
        with self.assertRaises(ValueError):
            store.reserve_all(self.db, T+86400, T+9, c)
        store.reserve_all(self.db, T+86400, T+86410, c)

    def test_late_response_persisted_but_not_scored(self):
        self.ingest()
        self.live_config()
        def clock():
            return T+121 if threading.current_thread().name.startswith('ThreadPool') else T+10
        with patch('phase8a.adapters.preflight'):
            self.run_cutoff(mode='live', call=adapters.fixture_invoke, clock=clock)
        self.assertTrue(all(r['status'] == 'error' for r in self.calls().values()))
        # Dispatch already late => no response; second case completes after deadline.
        other = self.path/'late.db'
        local = threading.local()
        def complete_late():
            if not threading.current_thread().name.startswith('ThreadPool'):
                return T+10
            local.count = getattr(local, 'count', 0)+1
            return T+10 if local.count == 1 else T+121
        with patch('phase8a.adapters.preflight'):
            runtime.run(other, self.news, self.c, T, 'live', complete_late, adapters.fixture_invoke)
        with store.connect(other, True) as con:
            rows = list(con.execute('SELECT status,result_json FROM calls'))
            self.assertTrue(any(r[0] == 'late' and json.loads(r[1])['raw'] for r in rows))

    def test_wrong_profile_other_symbols_and_bad_numbers(self):
        for key, value in (('kairos_profile', 'news_return'), ('symbol', 'DOGE-USD'),
                           ('daily_budget_usd', float('nan')), ('max_calls', True)):
            c = copy.deepcopy(self.c)
            c[key] = value
            with self.assertRaises(ValueError):
                core.validate_config(c)

    def test_profile_endpoint_and_same_state_requests(self):
        state = {'news': ['test'], 'cutoff': T}
        k = adapters.request('kairos', state, self.c)
        j = adapters.request('jev', state, self.c)
        self.assertEqual(k['state'], j['state'])
        self.assertEqual(k['questions'], j['questions'])
        self.assertEqual(k['task_profile'], 'general')
        self.assertEqual(k['model'], 'DMIND/Kairos')
        self.assertEqual(adapters.ENDPOINTS['kairos'], 'https://minara.cloud/v1/systemone')
        g = adapters.request('gpt6_astra', state, self.c)
        a = adapters.request('claude_fable', state, self.c)
        self.assertEqual(json.loads(g['input']), state)
        self.assertEqual(json.loads(a['messages'][0]['content']), state)
        self.assertNotIn('tools', g)
        self.assertNotIn('temperature', g)

    def test_confidence_is_not_probability_or_calibration(self):
        raw = adapters.fixture_invoke('kairos', {}, self.c)
        parsed = adapters.parse('kairos', raw)
        self.assertEqual(parsed['probability_up'], .65)
        self.assertEqual(parsed['provider_confidence'], .123)
        self.assertEqual(parsed['probability_kind'], 'uncalibrated')
        raw['calibration_version'] = 'provider-v1'
        self.assertIsNone(adapters.parse('kairos', raw)['calibration_id'])

    def test_invalid_probability_and_incomplete_outputs(self):
        for p in (True, float('nan'), -0.1, 1.1, '0.7'):
            with self.assertRaises(ValueError):
                core.normalize(p, 'model')
        raw = adapters.fixture_invoke('kairos', {}, self.c)
        raw['answers']['direction']['probabilities']['DOWN'] = .9
        with self.assertRaises(ValueError):
            adapters.parse('kairos', raw)
        raw = adapters.fixture_invoke('gpt6_astra', {}, self.c)
        raw['status'] = 'incomplete'
        with self.assertRaises(ValueError):
            adapters.parse('gpt6_astra', raw)

    def test_redaction_raw_and_exception_and_cli(self):
        self.ingest()
        secret = 'test-private-key-123'
        def call(e, s, c):
            raw = adapters.fixture_invoke(e, s, c)
            raw['diagnostic'] = secret
            raw['Authorization'] = 'Bearer '+secret
            return raw
        with patch.dict(os.environ, {'KAIROS_API_KEY': secret}):
            self.run_cutoff(call=call)
        self.assertNotIn(secret, core.canonical(self.calls()))
        out = io.StringIO()
        with redirect_stderr(out), patch('phase8a.cli.report', side_effect=RuntimeError(secret)):
            self.assertEqual(main(['report', '--db', str(self.db)]), 2)
        self.assertNotIn(secret, out.getvalue())

    def test_missing_keys_and_unknown_prices_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(adapters.ProviderError):
                adapters.preflight(self.c, {})
            self.live_config()
            with self.assertRaises(adapters.ProviderError):
                adapters.preflight(self.c, {})
        with patch.dict(os.environ, {k: 'test' for k in core.KEYS.values()}):
            self.c['engines']['kairos']['pricing_verified'] = False
            with self.assertRaises(adapters.ProviderError):
                adapters.preflight(self.c, {})

    def test_exact_price_match_idempotence_and_flat_semantics(self):
        self.ingest()
        self.run_cutoff()
        self.candles_for(end_price=100)
        self.assertIsNone(evaluation.prices(self.candles, T, 14400, T+14459))
        r = evaluation.prices(self.candles, T, 14400, T+15000)
        self.assertTrue(r['flat'])
        self.assertEqual(r['price_up'], 0)
        self.assertEqual(r['paper_entry_price'], 101)
        self.assertEqual(evaluation.evaluate(self.db, self.candles, lambda: T+15000)['evaluated'], 1)
        self.assertEqual(evaluation.evaluate(self.db, self.candles, lambda: T+15001)['evaluated'], 0)

    def test_missing_candle_not_interpolated(self):
        self.candles_for()
        con = sqlite3.connect(self.candles)
        con.execute('DELETE FROM candles_5m WHERE start_ts=?', (T+300,))
        con.commit()
        con.close()
        self.assertIsNone(evaluation.prices(self.candles, T, 14400, T+15000))

    def test_metrics_known_values_and_endpoints(self):
        s = evaluation.scores([(.5, 0), (.5, 1)])
        self.assertEqual(s['brier'], .25)
        self.assertEqual(s['accuracy'], .5)
        self.assertAlmostEqual(s['logloss'], math.log(2))
        self.assertTrue(math.isfinite(evaluation.scores([(0, 1), (1, 0)])['logloss']))
        self.assertIsNone(evaluation.scores([])['accuracy'])

    def test_nonoverlap_fixed_anchor_and_paper_costs(self):
        for t in (T, T+3600):
            self.ingest(available=t-10, published=t-120, title='Economic news '+str(t), guid=str(t), url='https://example.org/'+str(t))
            self.run_cutoff(t)
            self.candles_for(t)
        evaluation.evaluate(self.db, self.candles, lambda: T+20000)
        r = evaluation.report(self.db)
        self.assertEqual(r['paired_hourly_n'], 2)
        self.assertEqual(r['paired_nonoverlap_cutoffs'], [T])
        p = r['engines']['gpt6_astra']['paper']
        self.assertEqual(p['trades'], 1)
        self.assertAlmostEqual(p['pnl_after_trading_cost_usd'], 1000*(102/101-1-.002))
        self.assertEqual(r['engines']['claude_fable']['paper']['skip_rate'], 1)

    def test_future_candle_observation_not_used(self):
        self.candles_for()
        with sqlite3.connect(self.candles) as c:
            c.execute('UPDATE candles_5m SET fetched_at=? WHERE start_ts=?', (T+30000, T+14100))
        self.assertIsNone(evaluation.prices(self.candles, T, 14400, T+15000))

    def test_http_failure_has_no_automatic_retry_or_body_logging(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://minara.cloud/v1/systemone', 429,
                                                        'private diagnostic', {}, None)
        with patch.dict(os.environ, {'KAIROS_API_KEY': 'dummy'}), patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaises(adapters.ProviderError) as error:
                adapters.post('kairos', {}, 1)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(error.exception.http_status, 429)
        self.assertNotIn('private', str(error.exception))
        with self.assertRaises(adapters.ProviderError):
            adapters.NoRedirect().redirect_request(None, None, 307, '', {}, 'https://another.example')

    def test_cost_estimate_and_unknown_usage(self):
        settings = self.c['engines']['gpt6_astra']
        self.assertAlmostEqual(adapters.cost({'usage': {'input_tokens': 1000, 'output_tokens': 100}}, settings), .015)
        self.assertIsNone(adapters.cost({'usage': {}}, settings))
        self.assertEqual(adapters.redact({'bad': float('nan')})['bad'], '[NONFINITE]')

    def test_schema_required_fields_match_real_results(self):
        self.ingest()
        self.run_cutoff()
        schema = json.loads((ROOT/'phase8a/result.schema.json').read_text())
        for result in self.calls().values():
            self.assertTrue(set(schema['required']) <= set(result))
            self.assertTrue(set(result) <= set(schema['properties']))

    def test_existing_jev_sdk_adapter_with_local_transport(self):
        try:
            import httpx2
            import typesafe_sdk
        except ImportError:
            self.skipTest('existing production SDK contract exercised on Python 3.12 validation host')
        seen = []
        def handler(req):
            seen.append(json.loads(req.content))
            return httpx2.Response(200, json={'model': 'jev-latest',
                'usage': {'input_tokens': 100, 'output_tokens': 0, 'evaluated_questions': 1},
                'answers': {'direction': {'type': 'choice', 'choice': 'UP',
                'probabilities': {'UP': .7, 'DOWN': .3}, 'confidence': .1}}})
        original = typesafe_sdk.TypeSafeClient
        def factory(**kwargs):
            self.assertEqual(kwargs['retry'].max_retries, 0)
            return original(**kwargs, transport=httpx2.MockTransport(handler))
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'dummy'}), patch.object(typesafe_sdk, 'TypeSafeClient', side_effect=factory):
            raw = adapters.invoke('jev', {'cutoff': T, 'news': []}, self.c)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['state']['cutoff'], T)
        self.assertEqual(adapters.parse('jev', raw)['probability_up'], .7)

    def test_ten_minute_collection_configuration_and_invalid_interval(self):
        self.assertEqual(load_config(ROOT/'phase8a.news.config.example.json')['interval_seconds'], 600)
        self.assertEqual(load_config(ROOT/'news.config.example.json')['interval_seconds'], 900)
        for invalid in (True, 3600.0, 3601, 599):
            config = json.loads((ROOT/'news.config.example.json').read_text())
            config['interval_seconds'] = invalid
            path = self.path/'invalid.json'
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == '__main__':
    unittest.main()
