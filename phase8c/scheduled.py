"""Server-side hourly batches and independent mature-outcome evaluation. No trading."""
import argparse
import fcntl
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from . import pilot, store, market, evaluation
from .core import SYMBOLS, ENGINES, KEYS, canonical, digest, source_manifest, validate_config


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(canonical(value)+'\n')
    tmp.replace(path)


def validate_schedule(c):
    if set(c) != {'enabled', 'allow_unpriced_kairos', 'interval_seconds', 'start_cutoff',
                  'known_daily_budget_usd', 'known_30day_budget_usd', 'max_daily_calls', 'max_30day_calls',
                  'config', 'news_db', 'btc_candles_db', 'directory', 'additional_batches', 'baseline_root', 'technical_db'}:
        raise ValueError('schedule_fields')
    for k in ('enabled', 'allow_unpriced_kairos'):
        if type(c[k]) is not bool: raise ValueError('boolean_required')
    for k in ('interval_seconds','start_cutoff','max_daily_calls','max_30day_calls'):
        if type(c[k]) is not int or c[k] <= 0: raise ValueError('positive_integer_required')
    if c['interval_seconds'] % 300 or 14400 % c['interval_seconds'] or c['start_cutoff'] % c['interval_seconds']:
        raise ValueError('schedule_alignment')
    for k in ('known_daily_budget_usd','known_30day_budget_usd'):
        if isinstance(c[k],bool) or not isinstance(c[k],(int,float)) or not math.isfinite(c[k]) or c[k]<=0:
            raise ValueError('budget_required')
    if not isinstance(c['additional_batches'],list):raise ValueError('batch_paths_required')
    return c


def ledger(path):
    c = sqlite3.connect(path,timeout=10)
    c.row_factory=sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS batches(cutoff INTEGER PRIMARY KEY, reserved_at REAL,
                 known_reserved_usd REAL, calls INTEGER, status TEXT, directory TEXT)''')
    c.commit()
    return c


def reserve(path, cutoff, now, ceiling, policy, directory):
    """Shared durable reservation. Never refund uncertain calls and never replay a cutoff."""
    with closing(ledger(path)) as c, c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM batches WHERE cutoff=?',(cutoff,)).fetchone():
            return False
        latest=c.execute('SELECT max(reserved_at) FROM batches').fetchone()[0]
        if latest is not None and now < latest:raise ValueError('clock_regression')
        total=c.execute('SELECT coalesce(sum(known_reserved_usd),0),coalesce(sum(calls),0) FROM batches WHERE cutoff>?',(cutoff-30*86400,)).fetchone()
        daily=c.execute('SELECT coalesce(sum(known_reserved_usd),0),coalesce(sum(calls),0) FROM batches WHERE reserved_at>=?',
                        (int(now)//86400*86400,)).fetchone()
        if (total[0]+ceiling > policy['known_30day_budget_usd'] or daily[0]+ceiling > policy['known_daily_budget_usd']
            or total[1]+20 > policy['max_30day_calls'] or daily[1]+20 > policy['max_daily_calls']):
            raise ValueError('schedule_budget_or_call_limit')
        c.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)',(cutoff,now,ceiling,20,'reserved',str(directory)))
    return True


def predict(policy, clock=time.time, call=None):
    validate_schedule(policy)
    root=Path(policy['directory']);root.mkdir(parents=True,exist_ok=True)
    now=clock(); interval=policy['interval_seconds'];cutoff=int(now)//interval*interval
    if not policy['enabled']:return {'status':'disabled'}
    config=validate_config(json.loads(Path(policy['config']).read_text()))
    if config['horizon_seconds']!=14400:raise ValueError('scheduled_horizon_must_be_four_hours')
    if cutoff<policy['start_cutoff'] or not cutoff<=now<=cutoff+config['max_start_delay_seconds']:
        return {'status':'outside_start_window','cutoff':cutoff}
    with open(root/'predict.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=root/'schedule.sqlite3'
        with closing(ledger(path)) as c, c:
            c.execute('CREATE TABLE IF NOT EXISTS contract(id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT)')
            fingerprint=digest({'config':config,'sources':source_manifest()})
            old=c.execute('SELECT fingerprint FROM contract WHERE id=1').fetchone()
            if old is not None and old[0]!=fingerprint:raise ValueError('scheduled_contract_changed')
            c.execute('INSERT OR IGNORE INTO contract VALUES(1,?)',(fingerprint,))
            if c.execute('SELECT 1 FROM batches WHERE cutoff=?',(cutoff,)).fetchone():
                return {'status':'already_attempted_no_retry','cutoff':cutoff}
        # Failed preflight is also marked for this cutoff: no repeated preparation loop.
        try:
            plans,ceiling=pilot.prepare(config,policy['news_db'],cutoff,policy['allow_unpriced_kairos'],
                                        baseline_root=policy['baseline_root'],technical_db=policy['technical_db'])
            if ceiling>2.0:raise ValueError('batch_ceiling_limit')
            if clock()>cutoff+config['max_start_delay_seconds']:raise ValueError('late_preflight')
        except Exception as exc:
            reason=str(exc) if isinstance(exc,ValueError) and str(exc) in ({'no_eligible_news_'+s for s in SYMBOLS}|{'source_stale','late_preflight','batch_ceiling_limit','technical_history_missing_or_late','micro_coverage_incomplete','btc_trade_coverage_gap','trade_pagination_gap'}) else 'input_or_preflight_unavailable'
            with closing(ledger(path)) as c,c:
                c.execute('INSERT OR IGNORE INTO batches VALUES(?,?,?,?,?,?)',
                          (cutoff,clock(),0,0,'preflight_skipped',''))
            return {'status':'preflight_skipped','cutoff':cutoff,'reason':reason}
        directory=root/'runs'/str(cutoff)
        if not reserve(path,cutoff,clock(),ceiling,policy,directory):
            return {'status':'already_attempted_no_retry','cutoff':cutoff}
        result=pilot.run_batch(config,policy['news_db'],directory,cutoff,
                               allow_unpriced_kairos=policy['allow_unpriced_kairos'],clock=clock,call=call,
                               prepared=(plans,ceiling))
        # The original, actually dispatched inputs remain immutable. Mark the scheduled purpose separately.
        write_json(directory/'scheduled.json',{'purpose':'hourly_prospective','interval_seconds':interval,
                   'cutoff':cutoff,'source_hash':digest(source_manifest())})
        ok=sum(r['status']=='ok' for a in result['assets'].values() for r in a.get('results',[]))
        status='complete' if ok==20 else 'partial'
        with closing(ledger(path)) as c,c:c.execute('UPDATE batches SET status=? WHERE cutoff=?',(status,cutoff))
        return {'status':status,'cutoff':cutoff,'ok_calls':ok,'known_cost_estimate_usd':result['known_cost_estimate_usd'],
                'unknown_cost_calls':result['unknown_cost_calls']}


def batch_paths(policy):
    root=Path(policy['directory'])
    paths=[p.parent for p in (root/'runs').glob('*/manifest.json')]
    paths.extend(Path(p) for p in policy['additional_batches'])
    return sorted(set(paths))


def evaluate_batches(policy, clock=time.time):
    """Independent of prediction enable/budget gates; public price GETs only."""
    root=Path(policy['directory']);root.mkdir(parents=True,exist_ok=True)
    saved,errors=0,[]
    with open(root/'evaluate.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for directory in batch_paths(policy):
            if not (directory/'manifest.json').exists():continue
            try:
                m=json.loads((directory/'manifest.json').read_text())
            except Exception:
                errors.append({'directory':str(directory),'error':'invalid_batch_manifest'});continue
            if clock()<m['cutoff']+m['horizon_seconds']+60:continue
            for symbol in SYMBOLS:
                db=directory/(symbol+'.db')
                if not db.exists():continue
                try:
                    with store.connect(db,True) as c:
                        if not c.execute('SELECT 1 FROM cohorts WHERE cutoff NOT IN (SELECT cutoff FROM outcomes)').fetchone():continue
                    price_path=directory/'asset-prices.sqlite3'
                    if symbol!='BTC-USD':market.fetch(price_path,symbol,m['cutoff'],m['horizon_seconds'],clock)
                    r=evaluation.evaluate(db,policy['btc_candles_db'] if symbol=='BTC-USD' else price_path,clock)
                    saved+=r['evaluated']
                except Exception:
                    errors.append({'cutoff':m['cutoff'],'symbol':symbol,'error':'price_or_evaluation_unavailable'})
            try:pilot.summarize(directory)
            except Exception:errors.append({'cutoff':m['cutoff'],'error':'batch_summary_unavailable'})
        report=aggregate(policy,clock)
        report.update(newly_evaluated=saved,errors=errors)
        write_json(root/'report.json',report)
    return report


def aggregate(policy, clock=time.time):
    output={'as_of':clock(),'assets':{},'answers':[],'prediction_enabled':policy['enabled'],
            'kairos_cost_unknown':True,'known_daily_budget_usd':policy['known_daily_budget_usd'],
            'known_30day_budget_usd':policy['known_30day_budget_usd']}
    # Public numeric coverage diagnostics, no headlines or credentials.
    try:
        from news.store import snapshot
        from .core import freeze, SELECTION_VERSION
        config=validate_config(json.loads(Path(policy['config']).read_text()))
        snap=snapshot(policy['news_db'],output['as_of'],hours=config['lookback_hours'],deduplicate=False)
        output['news_policy']={'selection_version':SELECTION_VERSION,'since':policy['start_cutoff'],'allowed_sources':config['allowed_sources']}
        output['news_availability']={'as_of':output['as_of'],'sources':[
            {'id':r['feed_id'],'healthy':r['enabled'] and not r['stale'],
             'articles_24h':sum(n['source_id']==r['feed_id'] for n in snap['news'])}
            for r in snap['coverage'] if r['feed_id'] in config['allowed_sources']],
            'assets':{symbol:len(freeze(snap,{**config,'symbol':symbol},output['as_of'])['state']['news']) for symbol in SYMBOLS}}
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
        output['news_availability']={'status':'unavailable'}
    for symbol in SYMBOLS:
        records=[]
        for directory in batch_paths(policy):
            db=directory/(symbol+'.db')
            if not db.exists():continue
            with store.connect(db,True) as c:
                row=c.execute('SELECT cutoff FROM cohorts').fetchone()
                if row is None:continue
                t=row[0]
                rows=c.execute('SELECT engine,result_json FROM calls').fetchall()
                results={r[0]:json.loads(r[1]) if r[1] else {'status':'pending'} for r in rows}
                outcome=c.execute('SELECT outcome_json FROM outcomes WHERE cutoff=?',(t,)).fetchone()
                o=json.loads(outcome[0]) if outcome else None
                records.append((t,results,o))
                if o:
                    for engine,r in results.items():
                        p=r.get('probability_up')
                        s=evaluation.scores([(p,o['price_up'])]) if r['status']=='ok' else None
                        output['answers'].append({'symbol':symbol,'cutoff':t,'target_end':r.get('target_end'),
                            'engine':engine,'status':r['status'],'probability_up':p,'actual_up':o['price_up'],
                            'reference_price':o['reference_price'],'end_price':o['end_price'],
                            'correct':s['accuracy']==1 if s else None,'brier':s['brier'] if s else None,
                            'logloss':s['logloss'] if s else None})
        paired=[(t,r,o) for t,r,o in records if o and all(r.get(e,{}).get('status')=='ok' for e in ENGINES)]
        asset={'recorded':len(records),'evaluated':sum(o is not None for _,_,o in records),'engines':{}}
        for e in ENGINES:
            valid=sum(r.get(e,{}).get('status')=='ok' for _,r,_ in records)
            asset['engines'][e]={'paired_hourly_descriptive':evaluation.scores([(r[e]['probability_up'],o['price_up']) for t,r,o in paired]),
                'paired_nonoverlap_primary':evaluation.scores([(r[e]['probability_up'],o['price_up']) for t,r,o in paired if t%14400==0]),
                'unavailable_rate':1-valid/len(records) if records else None}
        output['assets'][symbol]=asset
    path=Path(policy['directory'])/'schedule.sqlite3'
    if path.exists():
        with closing(ledger(path)) as c:
            output['schedule']=[dict(r) for r in c.execute('SELECT cutoff,status,calls,known_reserved_usd FROM batches ORDER BY cutoff')]
    return output


def main():
    os.umask(0o077);logging.disable(logging.CRITICAL)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('predict','evaluate','check'))
    p.add_argument('--schedule',required=True)
    p.add_argument('--env-file')
    args=p.parse_args()
    try:
        if args.env_file:
            from dotenv import dotenv_values
            for k,v in dotenv_values(args.env_file,interpolate=False).items():
                if k in KEYS.values() and v:os.environ[k]=v
        policy=validate_schedule(json.loads(Path(args.schedule).read_text()))
        if args.command=='predict':
            out=predict(policy)
            write_json(Path(policy['directory'])/'prediction-status.json',out)
        elif args.command=='evaluate':
            r=evaluate_batches(policy);out={'newly_evaluated':r['newly_evaluated'],'errors':r['errors']}
        else:
            config=validate_config(json.loads(Path(policy['config']).read_text()))
            out={'valid':True,'enabled':policy['enabled'],'start_cutoff':policy['start_cutoff'],
                 'credentials_present':{e:bool(os.environ.get(k)) for e,k in KEYS.items()},
                 'horizon_seconds':config['horizon_seconds']}
        print(canonical(out));return 0
    except Exception:
        print(canonical({'status':'error','error':'scheduled_command_failed_no_model_retry'}));return 2


if __name__=='__main__':raise SystemExit(main())
