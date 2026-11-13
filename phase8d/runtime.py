"""Durable four-engine dispatch. Unknown-charge failures are never retried."""
import copy,json,time
from concurrent.futures import ThreadPoolExecutor
from . import store,events,adapters
from .core import ENGINES,canonical,digest,validate_config

def execute(db,news_db,technical_db,eid,policy,clock=time.time,provider=None):
    with store.connect(db) as c,c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
        if row is None or row['status']!='queued':return 'already_claimed'
        event=dict(row);c.execute("UPDATE events SET status='preparing' WHERE id=?",(eid,))
    config=copy.deepcopy(policy['model_config']);config['symbol']=event['symbol'];validate_config(config)
    try:
        if not 0<=clock()-event['cutoff']<=config['max_start_delay_seconds']:raise ValueError('late_event_no_replay')
        frozen=events.prepare(db,news_db,technical_db,event,config,policy['quotes_db'])
        if provider is None:adapters.preflight(config,frozen['state'],allow_unpriced_kairos=True)
        with store.connect(db) as c,c:
            c.execute('BEGIN IMMEDIATE');now=clock()
            if not event['cutoff']<=now<=event['cutoff']+config['max_start_delay_seconds']:raise ValueError('late_event_no_replay')
            day=c.execute('SELECT count(*),coalesce(sum(reserved_usd),0) FROM calls WHERE reserved_at>=?',(int(now)//86400*86400,)).fetchone()
            month=c.execute('SELECT coalesce(sum(reserved_usd),0) FROM calls WHERE reserved_at>=?',(now-30*86400,)).fetchone()[0]
            reserve=4*config['reserve_per_call_usd']
            if day[0]+4>policy['max_daily_calls'] or day[1]+reserve>policy['daily_known_budget_usd'] or month+reserve>policy['rolling30_known_budget_usd']:raise ValueError('budget_limit')
            c.execute("UPDATE events SET input_json=?,reference_quote=?,status='dispatched' WHERE id=?",(canonical(frozen),frozen['reference_quote']['id'],eid))
            c.executemany('INSERT INTO calls VALUES(?,?,?,?,?,NULL)',[(eid,e,'reserved',now,config['reserve_per_call_usd']) for e in ENGINES])
    except Exception as exc:
        reason=exc.code if isinstance(exc,adapters.ProviderError) else (str(exc) if isinstance(exc,ValueError) else 'input_preparation_failed')
        with store.connect(db) as c,c:c.execute("UPDATE events SET status='unavailable',error=? WHERE id=?",(adapters.redact(reason),eid))
        return 'unavailable'
    def worker(engine):
        r=dict(engine=engine,status='error',requested_at=clock(),completed_at=None,probability_up=None,
          probability_kind='uncalibrated',news_set_id=frozen['state']['news_set_id'],input_id=frozen['input_id'],
          cutoff=event['cutoff'],target_end=event['target'],configured_model=config['engines'][engine]['model'],
          error=None,cost_usd=None,cost_kind='unknown',kairos_profile='general' if engine=='kairos' else None)
        start=time.monotonic()
        try:
            if not event['cutoff']<=r['requested_at']<=event['cutoff']+config['deadline_seconds']:raise adapters.ProviderError('dispatch_deadline')
            req=adapters.request(engine,frozen['state'],config);r.update(request_json=req,request_hash=digest(req))
            raw=(provider or adapters.invoke)(engine,json.loads(canonical(frozen['state'])),config)
            r['completed_at']=clock();r['raw']=adapters.redact(raw);r.update(adapters.parse(engine,raw))
            r['cost_usd']=adapters.cost(raw,config['engines'][engine]);r['cost_kind']='tariff_estimate' if r['cost_usd'] is not None else 'unknown'
            r['status']='ok'
            if r['completed_at']<r['requested_at']:r.update(status='error',error='clock_moved_backwards')
            elif r['completed_at']>event['cutoff']+config['deadline_seconds']:r.update(status='late',error='completion_deadline')
        except adapters.ProviderError as exc:r.update(error=exc.code,http_status=exc.http_status)
        except Exception:r['error']='provider_or_output_invalid'
        r['completed_at']=r['completed_at'] or clock();r['latency_ms']=(time.monotonic()-start)*1000
        with store.connect(db) as c,c:c.execute('UPDATE calls SET status=?,result_json=? WHERE event=? AND engine=? AND result_json IS NULL',(r['status'],canonical(adapters.redact(r)),eid,engine))
        return r['status']
    with ThreadPoolExecutor(max_workers=4) as pool:statuses=list(pool.map(worker,ENGINES))
    status='complete' if all(s=='ok' for s in statuses) else 'partial'
    with store.connect(db) as c,c:c.execute('UPDATE events SET status=? WHERE id=?',(status,eid))
    return status

def recover(db):
    # A crash may have charged a provider. Preserve reservations, never resend.
    with store.connect(db) as c,c:
        c.execute("UPDATE events SET status='interrupted',error='restart_no_retry' WHERE status IN ('preparing','dispatched')")
        c.execute("UPDATE calls SET status='unknown' WHERE status='reserved'")
