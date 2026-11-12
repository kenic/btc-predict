"""One cutoff, one stored snapshot, four concurrent calls, no backfill or automatic retry."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from news.store import snapshot
from . import adapters, store
from .core import ENGINES, VERSION, canonical, digest, freeze, validate_config, number


def result_base(engine, cutoff, frozen, config):
    return dict(schema=VERSION, symbol=config['symbol'], engine=engine, configured_model=config['engines'][engine]['model'],
                model_version=None, provider_model_version=None, direction=None, probability_up=None,
                probability_kind='uncalibrated', calibration_id=None, provider_confidence=None,
                news_set_id=frozen['state']['news_set_id'], input_id=frozen['input_id'],
                experiment='phase8b', baseline_input_id=frozen.get('baseline_input_id'),
                technical_snapshot_id=frozen['state'].get('technical',{}).get('snapshot_id'),
                news_versions=[{'article_id': n['article_id'], 'version_id': n['version_id']}
                               for n in frozen['state']['news']],
                cutoff=cutoff, target_end=cutoff+config['horizon_seconds'],
                requested_at=None, completed_at=None, latency_ms=None, error=None, http_status=None,
                raw=None, usage=None, cost_usd=None, cost_kind='unknown',
                request_hash=None, request_json=None,
                kairos_profile=config['kairos_profile'] if engine == 'kairos' else None)


def run(db, news_db, config, cutoff=None, mode='offline', clock=time.time, call=None, *, allow_unpriced_kairos=False, frozen_input=None):
    config = validate_config(config)
    now = number(clock())
    cutoff = int(now)//config['interval_seconds']*config['interval_seconds'] if cutoff is None else number(cutoff)
    if cutoff != int(cutoff) or cutoff % config['interval_seconds'] or cutoff > now:
        raise ValueError('unaligned_or_future_cutoff')
    cutoff = int(cutoff)
    if mode not in ('offline', 'live'):
        raise ValueError('invalid_mode')
    if mode == 'live' and now-cutoff > config['max_start_delay_seconds']:
        raise ValueError('late_start_no_historical_live_calls')
    store.initialize(db, config)
    with store.connect(db, True) as c:
        prior = c.execute('SELECT mode FROM cohorts LIMIT 1').fetchone()
        if prior and prior[0] != mode:
            raise ValueError('offline_live_database_mixing')
        if c.execute('SELECT 1 FROM cohorts WHERE cutoff=?', (cutoff,)).fetchone():
            return {'cutoff': cutoff, 'status': 'already_recorded_no_retry'}
    if frozen_input is None or 'technical' not in frozen_input['state']:
        raise ValueError('phase8b_requires_frozen_technical_input')
    frozen = frozen_input
    if frozen['config_hash'] != digest(config) or frozen['state']['cutoff'] != cutoff:
        raise ValueError('frozen_input_contract_mismatch')
    if allow_unpriced_kairos:
        frozen['unpriced_kairos_once_authorized'] = True
    if not store.add_cohort(db, cutoff, now, frozen, mode):
        return {'cutoff': cutoff, 'status': 'already_recorded_no_retry'}
    reason = None
    if not frozen['state']['news']:
        reason = 'no_eligible_news'
    elif any(r['enabled'] and r['stale'] and r['feed_id'] in config['allowed_sources']
             for r in frozen['snapshot']['coverage']):
        reason = 'source_stale'
    if not reason and mode == 'live':
        try:
            adapters.preflight(config, frozen['state'], allow_unpriced_kairos=allow_unpriced_kairos)
            if not cutoff <= clock() <= cutoff+config['max_start_delay_seconds']:
                raise ValueError('late_start')
            store.reserve_all(db, cutoff, clock(), config)
        except (adapters.ProviderError, ValueError) as exc:
            # Only our enumerated errors, never SDK exception text.
            reason = exc.code if isinstance(exc, adapters.ProviderError) else str(exc)
    if reason:
        for e in ENGINES:
            r = result_base(e, cutoff, frozen, config)
            r.update(status='skipped', error=reason)
            store.finish(db, cutoff, e, r)
        store.finish_cohort(db, cutoff, 'skipped')
        return {'cutoff': cutoff, 'status': 'skipped', 'reason': reason, 'news_count': len(frozen['state']['news'])}

    def worker(engine):
        r = result_base(engine, cutoff, frozen, config)
        r['requested_at'] = clock()
        started = time.monotonic()
        r['status'] = 'error'
        try:
            if mode == 'live' and not cutoff <= r['requested_at'] <= cutoff+config['deadline_seconds']:
                raise adapters.ProviderError('dispatch_deadline')
            r['request_json'] = adapters.request(engine, frozen['state'], config)
            r['request_hash'] = digest(r['request_json'])
            provider = call or (adapters.invoke if mode == 'live' else adapters.fixture_invoke)
            # Independent deserialization prevents an adapter mutating another engine's input.
            raw = provider(engine, json.loads(canonical(frozen['state'])), config)
            r['completed_at'] = clock()
            r['raw'] = adapters.redact(raw)
            r['usage'] = r['raw'].get('usage')
            r['cost_usd'] = adapters.cost(raw, config['engines'][engine]) if mode == 'live' else 0.0
            r['cost_kind'] = 'tariff_estimate' if mode == 'live' and r['cost_usd'] is not None else ('fixture' if mode == 'offline' else 'unknown')
            r.update(adapters.parse(engine, raw))
            r['status'] = 'ok'
            if r['completed_at'] < r['requested_at']:
                r.update(status='error', error='clock_moved_backwards')
            elif mode == 'live' and r['completed_at'] > cutoff+config['deadline_seconds']:
                r.update(status='late', error='completion_deadline')
        except adapters.ProviderError as exc:
            r.update(error=exc.code, http_status=exc.http_status)
        except Exception:
            r.update(error='provider_or_output_invalid')
        r['completed_at'] = r['completed_at'] or clock()
        r['latency_ms'] = (time.monotonic()-started)*1000
        # Redact ALL fields, including returned model identifiers and normalized metadata.
        store.finish(db, cutoff, engine, adapters.redact(r))
        return r['status']

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(worker, ENGINES))
    status = 'complete' if all(s == 'ok' for s in statuses) else 'partial'
    store.finish_cohort(db, cutoff, status)
    return {'cutoff': cutoff, 'status': status, 'mode': mode, 'news_count': len(frozen['state']['news']),
            'input_id': frozen['input_id']}
