"""Phase 7: 60-day prospective, frozen selector; paper execution only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import time
from phase4 import config_hash
from phase6 import aligned_features, gate, implementation_hashes
from phase6_methods import BASE_METHODS, METHODS, ensembles, logistic, output, rule
from phase7_model import features, choose, train, frozen_logistic, quote_pnl
from volatility import probabilities, winner

ROOT=Path(__file__).resolve().parent
OWN_SOURCES=('phase7.py','phase7_model.py','phase7_report.py','PHASE7.md',
             'deploy/systemd/btc-phase7.service','deploy/systemd/btc-phase7.timer')
PRIMARY_METHODS=('selector','gpt','order_flow','logistic','ensemble_vote','ensemble_probability','train_best_fixed','cash')

@contextmanager
def database(path, readonly=False):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro' if readonly else str(path),uri=readonly,timeout=5)
    c.row_factory=sqlite3.Row
    try:
        with c: yield c
    finally: c.close()

def stamp(): return datetime.now(timezone.utc).isoformat()
def epoch(s):return datetime.fromisoformat(s).timestamp()
def hashes():
    names=set(implementation_hashes())|set(OWN_SOURCES)|{'phase4.py','db.py'}
    return {n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in sorted(names)}
def dependencies():
    return {n:importlib.metadata.version(n) for n in ('numpy','openai','requests','pandas','python-dotenv')}

def schema(path):
    with database(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          armed_at TEXT NOT NULL, arm_json TEXT NOT NULL, arm_hash TEXT NOT NULL,
          frozen_at TEXT, frozen_json TEXT, frozen_hash TEXT, start_target INTEGER, end_exclusive INTEGER);
        CREATE TABLE IF NOT EXISTS events(target INTEGER PRIMARY KEY, status TEXT NOT NULL,
          started_at REAL NOT NULL, context TEXT, snapshot_hash TEXT, features_json TEXT,
          gate TEXT, reason TEXT, single_class TEXT, active_votes INTEGER,
          ready_at REAL, selection TEXT, predictions_json TEXT,
          entry_json TEXT, exit_json TEXT, execution_status TEXT,
          actual_return REAL, actual_direction TEXT, evaluated_at REAL);
        CREATE TABLE IF NOT EXISTS calls(target INTEGER,kind TEXT,slot INTEGER,status TEXT,
          started_at REAL,completed_at REAL,model TEXT,payload_json TEXT,error_type TEXT,
          PRIMARY KEY(target,kind,slot));
        CREATE TABLE IF NOT EXISTS training(singleton INTEGER PRIMARY KEY CHECK(singleton=1),snapshot_json TEXT NOT NULL,snapshot_hash TEXT NOT NULL);
        ''')

def arm(path,source,market,candles):
    # An idempotent arm never resets a started or waiting experiment.
    schema(path)
    with database(path) as c:
        existing=c.execute('SELECT * FROM config').fetchone()
        if existing:
            validate(existing); return
    with database(source,True) as c:
        saved=c.execute('SELECT * FROM phase6_config').fetchone()
        cfg=json.loads(saved['config_json'])
        if config_hash(cfg)!=saved['config_hash'] or cfg['implementation_hashes']!=implementation_hashes():
            raise RuntimeError('Original Phase 6 hashes differ')
    dirty=subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,text=True)
    if dirty.strip():raise RuntimeError('Commit tracked code before arming')
    spec=dict(version='phase7-v1',days=60,source=str(Path(source).resolve()),market=str(Path(market).resolve()),
              candles=str(Path(candles).resolve()),source_config_hash=saved['config_hash'],
              phase6_design=cfg,source_hashes=hashes(),dependencies=dependencies(),
              code_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              primary_comparison='selector minus order_flow mean net bp per accepted executable opportunity',
              cost_bps=5,cost_sensitivity=[0,2,5,10,20],quote_max_delay_seconds=15,
              prediction_deadline_seconds=600,call_start_deadline_seconds=480,
              logistic='Frozen fit on all labeled Phase6+6b snapshots, including rejected gates; no Phase7 updates',
              stop='60 calendar days, no performance-based stopping, extensions or replacements',
              success='At fixed end: primary day-block 95% lower CI >0 and mean improvement >=1bp; otherwise inconclusive/not supported',
              primary_ci='UTC calendar-day moving-block bootstrap, 3-day blocks, 10000 replicates, seed 72026; diagnostics 1-day and 7-day blocks',
              execution='Common ready time, first bid/ask <=15s after ready; exit first bid/ask <=15s after target+3600; unit paper long/short',
              missing='No prediction retry/backfill. No entry quote => no executable opportunity; missing exit => unknown PnL, final verdict withheld',
              min_days=30,min_executable=100,min_gate_coverage=.95,require_all_accepted_execution_complete=True)
    with database(path) as c:
        c.execute('INSERT INTO config(singleton,armed_at,arm_json,arm_hash) VALUES(1,?,?,?)',
                  (stamp(),json.dumps(spec,sort_keys=True),config_hash(spec)))

def validate(state):
    a=json.loads(state['arm_json'])
    if config_hash(a)!=state['arm_hash'] or a['source_hashes']!=hashes() or a['dependencies']!=dependencies():
        raise RuntimeError('Frozen code/config/dependencies changed')
    if state['frozen_json']:
        f=json.loads(state['frozen_json'])
        if config_hash(f)!=state['frozen_hash']:
            raise RuntimeError('Frozen model hash mismatch')
        expected_start=(int(f['fit_cutoff'])//3600+1)*3600
        if state['start_target']!=expected_start or state['end_exclusive']!=expected_start+a['days']*86400:
            raise RuntimeError('Frozen observation window changed')
    return a

def first_quote(c,at,max_delay=15):
    q=c.execute('SELECT timestamp,best_bid,best_ask FROM orderbook_samples WHERE timestamp>=? AND timestamp<=? ORDER BY timestamp LIMIT 1',(at,at+max_delay)).fetchone()
    if q is None:return None
    q=dict(q)
    if not all(math.isfinite(q[k]) and q[k]>0 for k in ('best_bid','best_ask')) or q['best_bid']>q['best_ask']:return None
    return q

def initialize(path,clock=None):
    clock=time.time() if clock is None else clock
    with database(path,True) as c:state=c.execute('SELECT * FROM config').fetchone()
    a=validate(state)
    if state['frozen_json']:return True
    events=[];predictions=[];training=[];configs=[]
    with database(a['source'],True) as c:
        c.execute('BEGIN')
        for ph,expected in (('phase6',96),('phase6b',104)):
            ee=[dict(e) for e in c.execute('SELECT * FROM '+ph+'_events ORDER BY target')]
            accepted=[e for e in ee if e['gate']=='accepted']
            if len(accepted)!=expected: return False
            if any(e['actual_return'] is None or e['evaluated_at'] is None for e in accepted):return False
            cfg=dict(c.execute('SELECT * FROM '+ph+'_config').fetchone());configs.append(cfg)
            if config_hash(json.loads(cfg['config_json']))!=cfg['config_hash']:raise RuntimeError('Source config mismatch')
            if cfg['config_hash']!=a['source_config_hash']:raise RuntimeError('Source design changed')
            events.extend(dict(e,cohort=ph) for e in ee)
            predictions.extend(dict(p,cohort=ph) for p in c.execute('SELECT * FROM '+ph+'_predictions ORDER BY target,method'))
            for e in ee:
                if e['actual_direction'] in ('UP','DOWN') and e['evaluated_at'] and e['features_json']:
                    training.append(dict(id=ph+':'+str(e['target']),target=e['target'],available_at=epoch(e['evaluated_at']),
                                         outcome=e['actual_direction'],features=json.loads(e['features_json']),snapshot_hash=e['snapshot_hash']))
    indexes={(p['target'],p['method']):p for p in predictions}
    samples=[];quotes=[]
    with database(a['market'],True) as c:
        c.execute('BEGIN')
        for e in sorted((e for e in events if e['gate']=='accepted'),key=lambda e:e['target']):
            pp={m:indexes[e['target'],m] for m in ('gpt','order_flow','logistic','ensemble_vote','ensemble_probability')}
            ready=max(epoch(p['completed_at']) for p in pp.values())
            entry=first_quote(c,ready);exit_quote=first_quote(c,e['target']+3600)
            if not entry or not exit_quote:raise RuntimeError('Historical execution quote missing; do not silently drop a training target')
            net={m:quote_pnl(pp[m]['direction'] if pp[m]['status']=='complete' else None,entry,exit_quote,5) for m in ('gpt','order_flow','logistic')};net['cash']=0.0
            samples.append(dict(target=e['target'],available_at=max(epoch(e['evaluated_at']),exit_quote['timestamp']),
                                x=features(json.loads(e['features_json']),pp['gpt']),net=net))
            quotes.append(dict(target=e['target'],ready=ready,entry=entry,exit=exit_quote))
    if max(r['available_at'] for r in samples)>=clock:return False
    learned=train(samples)
    f=next(r['features'] for r in training if all(r['features'].get(k) is not None for k in ('return_1h','return_5m','return_15m','sma_gap','macd_hist','buy_ratio','imbalance','micro_offset')))
    lr=logistic(f,training,clock)
    if lr['status']!='complete':raise RuntimeError('Cannot fit frozen logistic')
    learned['logistic']=lr['audit'];learned['fit_cutoff']=clock
    snapshot=dict(configs=configs,events=events,predictions=predictions,quotes=quotes,selector_samples=samples)
    raw=json.dumps(snapshot,sort_keys=True,allow_nan=False)
    learned['training_snapshot_hash']=hashlib.sha256(raw.encode()).hexdigest()
    learned['accepted_membership']=[r['target'] for r in samples]
    start=(int(clock)//3600+1)*3600
    if start<=max(r['target']+3600 for r in samples):raise RuntimeError('Training overlaps prospective start')
    with database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT frozen_json FROM config').fetchone()[0]:return True
        c.execute('INSERT INTO training VALUES(1,?,?)',(raw,learned['training_snapshot_hash']))
        c.execute('UPDATE config SET frozen_at=?,frozen_json=?,frozen_hash=?,start_target=?,end_exclusive=? WHERE singleton=1',
                  (stamp(),json.dumps(learned,sort_keys=True),config_hash(learned),start,start+60*86400))
    return True

def call_saved(path,target,kind,slot,fn,deadline):
    with database(path) as c:
        c.execute('INSERT INTO calls VALUES(?,?,?,\'started\',?,NULL,NULL,NULL,NULL)',(target,kind,slot,time.time()))
    try:
        result=fn();completed=time.time()
        status='complete' if completed<=deadline else 'late'
        model=result[1] if kind=='class' else result.get('model_version')
        with database(path) as c:c.execute('UPDATE calls SET status=?,completed_at=?,model=?,payload_json=? WHERE target=? AND kind=? AND slot=?',
                                          (status,completed,model,json.dumps(result),target,kind,slot))
        return result if status=='complete' else None
    except Exception as exc:
        # Never persist provider exception text, headers or credentials.
        with database(path) as c:c.execute('UPDATE calls SET status=\'failed\',completed_at=?,error_type=? WHERE target=? AND kind=? AND slot=?',
                                          (time.time(),type(exc).__name__,target,kind,slot))
        return None

def run(path,target,clock=None,builder=None,class_call=None,gpt_call=None):
    from phase4_runner import build_snapshot
    from next_stages import class_call as original_class
    from phase6 import direction_call
    clock=time.time() if clock is None else clock
    builder=builder or build_snapshot;class_call=class_call or original_class;gpt_call=gpt_call or direction_call
    with database(path) as c:
        state=c.execute('SELECT * FROM config').fetchone();a=validate(state)
        if not state['frozen_json']:return
        if target%3600 or not state['start_target']<=target<state['end_exclusive'] or not 105<=clock-target<=480:return
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM events WHERE target=?',(target,)).fetchone():return
        c.execute('INSERT INTO events(target,status,started_at) VALUES(?,\'started\',?)',(target,time.time()))
    learned=json.loads(state['frozen_json']);deadline=min(target+600,state['end_exclusive'])
    try:
        context=builder(target)[0];f=aligned_features(context,target)
        with database(path) as c:c.execute('UPDATE events SET context=?,snapshot_hash=?,features_json=? WHERE target=?',
                                          (context,hashlib.sha256(context.encode()).hexdigest(),json.dumps(f),target))
        if time.time()>target+480:raise RuntimeError('Snapshot too late')
        cfg=a['phase6_design'];source=dict(context=context,model_version=cfg['gate']['model'],predictor='openai',
            config_json=json.dumps(cfg['gate']['volatility_config']),target_candle_time=target,timeout=120)
        def one(slot):
            if time.time()>target+480:return None
            r=call_saved(path,target,'class',slot,lambda:class_call(source),deadline)
            return winner(probabilities(r[0])) if r is not None else None
        single=one(0);repeated=[]
        if single=='ACTIVE':
            with ThreadPoolExecutor(max_workers=10) as pool:repeated=list(pool.map(one,range(1,11)))
        valid=single is not None and (single!='ACTIVE' or len(repeated)==10 and all(v is not None for v in repeated))
        accepted,reason,votes=gate(single,repeated)
        if not valid or time.time()>deadline:
            with database(path) as c:c.execute('UPDATE events SET status=\'failed\',gate=\'unknown\',reason=? WHERE target=?',('Gate incomplete or late',target))
            return
        with database(path) as c:c.execute('UPDATE events SET gate=?,reason=?,single_class=?,active_votes=? WHERE target=?',
                                          ('accepted' if accepted else 'rejected',reason,single,votes,target))
        if not accepted:
            with database(path) as c:c.execute('UPDATE events SET status=\'complete\' WHERE target=?',(target,))
            return
        results={}
        def method(m):
            if time.time()>target+480:return output(reason='No time for direction; no retry')
            if m=='gpt':return call_saved(path,target,'direction',0,lambda:gpt_call(context,cfg['gate']['model']),deadline) or output(reason='GPT failed or late')
            if m=='logistic':return frozen_logistic(f,learned['logistic'])
            return rule(m,f,target=target)
        def safely(m):
            try:return method(m)
            except Exception as exc:return output(reason=type(exc).__name__)
        with ThreadPoolExecutor(max_workers=len(BASE_METHODS)) as pool:
            results=dict(zip(BASE_METHODS,pool.map(safely,BASE_METHODS)))
        results.update(ensembles(results))
        selection=choose(features(f,results['gpt']),learned['selector'])
        results['cash']=output(reason='Cash')
        results['selector']=dict(results[selection],selected_model=selection)
        results['train_best_fixed']=dict(results[learned['train_best_fixed']],selected_model=learned['train_best_fixed'])
        ready=time.time()
        if ready>deadline:
            results={m:output(reason='Common decision late') for m in results}
        with database(path) as c:c.execute('UPDATE events SET status=\'complete\',ready_at=?,selection=?,predictions_json=?,execution_status=? WHERE target=?',
                                          (ready,selection,json.dumps(results),'late_no_trade' if ready>deadline else 'pending',target))
    except Exception as exc:
        with database(path) as c:c.execute('UPDATE events SET status=\'failed\',reason=? WHERE target=?',(type(exc).__name__,target))

def evaluate(path,clock=None):
    clock=time.time() if clock is None else clock
    with database(path,True) as c:
        state=c.execute('SELECT * FROM config').fetchone();a=validate(state)
        events=[dict(e) for e in c.execute("SELECT * FROM events WHERE gate='accepted' AND (actual_return IS NULL OR execution_status IN ('pending','entry_saved'))")]
    with database(a['market'],True) as market,database(a['candles'],True) as candles:
        for e in events:
            entry=json.loads(e['entry_json']) if e['entry_json'] else None
            exit_quote=json.loads(e['exit_json']) if e['exit_json'] else None
            status=e['execution_status']
            if status in (None,'pending') and e['ready_at'] and clock>e['ready_at']+30:
                entry=first_quote(market,e['ready_at']);status='entry_saved' if entry else 'no_entry_quote'
            if status=='entry_saved' and clock>e['target']+3630:
                exit_quote=first_quote(market,e['target']+3600);status='complete' if exit_quote else 'missing_exit_quote'
            actual=e['actual_return'];direction=e['actual_direction'];at=e['evaluated_at']
            if actual is None and clock>=e['target']+3660:
                candle=candles.execute('SELECT close FROM candles_5m WHERE start_ts=?',(e['target']+3300,)).fetchone()
                if candle and e['features_json']:
                    reference=json.loads(e['features_json'])['reference'];actual=100*(candle['close']/reference-1)
                    direction='UP' if actual>0 else 'DOWN' if actual<0 else 'FLAT';at=clock
            with database(path) as c:c.execute('UPDATE events SET entry_json=?,exit_json=?,execution_status=?,actual_return=?,actual_direction=?,evaluated_at=? WHERE target=?',
                                               (json.dumps(entry) if entry else None,json.dumps(exit_quote) if exit_quote else None,status,actual,direction,at,e['target']))

def cycle(path):
    with database(path,True) as c:
        state=c.execute('SELECT * FROM config').fetchone();a=validate(state)
    if not initialize(path):print('Waiting for all 200 Phase6/6b outcomes');return
    with database(path) as c:
        c.execute("UPDATE events SET status='interrupted',reason='Interrupted reservation; no retry' WHERE status='started'")
        c.execute("UPDATE calls SET status='interrupted',error_type='Interrupted' WHERE status='started'")
    evaluate(path)
    run(path,int(time.time())//3600*3600)
    evaluate(path)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('arm','cycle','status'))
    p.add_argument('--db',type=Path,default=ROOT/'phase7.db');p.add_argument('--source',type=Path,default=ROOT/'btc.db')
    p.add_argument('--market',type=Path,default=ROOT/'market.db');p.add_argument('--candles',type=Path,default=ROOT/'candles.db');args=p.parse_args()
    if args.action=='status':
        from phase7_report import report
        print(json.dumps(report(args.db),indent=2));return
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    with open(str(args.db)+'.lock','a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        if args.action=='arm':arm(args.db,args.source,args.market,args.candles)
        else:cycle(args.db)
if __name__=='__main__':main()
