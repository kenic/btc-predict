"""Event forecast daemon and independent evaluation; never place an exchange order."""
import argparse,copy,fcntl,json,os,time,logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from news.store import snapshot
from . import store,events,runtime,evaluation,adapters
from .core import SYMBOLS,KEYS,canonical,digest,relevance,validate_config

def load(path):
    p=json.loads(Path(path).read_text());validate_config(p['model_config'])
    for k in ('max_daily_calls','daily_known_budget_usd','rolling30_known_budget_usd','fee_bps_per_side','slippage_bps_per_side'):
        if type(p[k]) not in (int,float) or p[k]<=0:raise ValueError('invalid_policy')
    if p['model_config']['trade_threshold']!=.55:raise ValueError('threshold_contract')
    if not isinstance(p['enabled'],bool):raise ValueError('invalid_enabled')
    return p

def smoke(db,policy):
    # Explicit connectivity validation with current archived news; excluded from event scores.
    T=time.time();snap=snapshot(policy['news_db'],T);ids=[]
    with store.connect(db) as c,c:
        for symbol in SYMBOLS:
            items=[n for n in snap['news'] if relevance(n,symbol)=='direct'][:1]
            if not items:items=[n for n in snap['news'] if relevance(n,symbol)][:1]
            if not items:raise ValueError('no_validation_news')
            eid=digest(['validation',symbol,T]);ids.append(eid)
            c.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',(eid,symbol,T,T+14400,'validation','queued',None,canonical(items),None,T,None))
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(lambda eid:runtime.execute(db,policy['news_db'],policy['technical_db'],eid,policy),ids))
    return {'events':ids,'statuses':statuses,'excluded_from_scores':True}

def main():
    os.umask(0o077);logging.disable(logging.CRITICAL)
    p=argparse.ArgumentParser();p.add_argument('command',choices=('init','predict','evaluate','smoke','check'));p.add_argument('--policy',required=True);p.add_argument('--env-file');a=p.parse_args()
    policy=load(a.policy);db=Path(policy['db']);db.parent.mkdir(parents=True,exist_ok=True)
    if a.env_file:
        from dotenv import dotenv_values
        for k,v in dotenv_values(a.env_file,interpolate=False).items():
            if k in KEYS.values() and v:os.environ[k]=v
    store.initialize(db,policy,time.time())
    if a.command=='init':events.bootstrap(db,policy['news_db']);print('{"initialized":true}');return
    if a.command=='check':
        print(canonical({'credentials_present':{e:bool(os.environ.get(k)) for e,k in KEYS.items()},'enabled':policy['enabled'],'symbols':SYMBOLS}));return
    if a.command=='smoke':print(canonical(smoke(db,policy)));return
    lock=open(str(db)+'.'+a.command+'.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if a.command=='predict':
        events.bootstrap(db,policy['news_db']);runtime.recover(db)
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending={}
            while True:
                try:
                    # Runtime switch does not mutate the frozen experiment contract.
                    enabled=not Path(str(db)+'.paused').exists() and policy['enabled']
                    if enabled:
                        events.discover(db,policy['news_db'])
                        with store.connect(db,True) as c:rows=c.execute("SELECT id FROM events WHERE status='queued' ORDER BY cutoff,id").fetchall()
                        for row in rows:
                            if row['id'] not in pending and len(pending)<4:
                                pending[row['id']]=pool.submit(runtime.execute,db,policy['news_db'],policy['technical_db'],row['id'],policy)
                    for eid in list(pending):
                        if pending[eid].done():
                            try:pending[eid].result()
                            except Exception:
                                with store.connect(db) as c,c:c.execute("UPDATE events SET status='interrupted',error='worker_failed_no_retry' WHERE id=?",(eid,))
                            del pending[eid]
                    store.heartbeat(db,'predict','ok' if enabled else 'paused',time.time())
                except Exception:store.heartbeat(db,'predict','discovery_failed',time.time())
                time.sleep(1)
    else:
        while True:
            try:evaluation.evaluate(db,policy);store.heartbeat(db,'evaluate','ok',time.time())
            except Exception:store.heartbeat(db,'evaluate','evaluation_failed',time.time())
            time.sleep(5)

if __name__=='__main__':main()
