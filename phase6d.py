"""Independent Claude ACTIVE gate and accepted-only direction observation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
from importlib.metadata import version as package_version
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from claude_observation import db, digest, stamp, provider, parse as parse_direction, prompt_literal
from volatility import CLASSES, probabilities, winner, classify, realized_volatility, brier
from phase6 import gate, aligned_features

ROOT = Path(__file__).resolve().parent
END = 1793577600  # 2026-11-02 00:00 UTC, exclusive
DEADLINE = 600
PARAMS = {'model':'claude-opus-5-5','max_tokens':4096,
          'thinking':{'type':'adaptive'},'output_config':{'effort':'medium'}}
SOURCES = ('phase6d.py','claude_observation.py','phase6.py','phase6_methods.py',
           'phase4_runner.py','next_stages.py','phase4.py','volatility.py',
           'predict_gpt.py','indicators.py','microstructure.py','db.py','candle_store.py','requirements.txt')


def class_prompt(config):
    from phase4_runner import instructions
    return instructions(config)+'\nReturn JSON only: {"p_quiet":0.33,"p_normal":0.34,"p_active":0.33,"reason":"short explanation"}\nMARKET DATA:\n'


def specification(volatility_config):
    quiet, active = volatility_config['quiet_upper'], volatility_config['active_lower']
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (quiet,active)) or not 0 <= quiet < active:
        raise ValueError('Invalid frozen volatility thresholds')
    return dict(version='phase6d-v1', params=PARAMS, volatility_config=volatility_config,
        api_version='2023-06-01', endpoint='https://api.anthropic.com/v1/messages', timeout=120, retries=0,
        prompts={'class':class_prompt(volatility_config), 'direction':prompt_literal('phase6.py','direction_call')},
        gate={'single':'ACTIVE','repeats':10,'valid_required':10,'active_votes_min':8,'tie_order':list(CLASSES)},
        direction_scope='Claude-accepted targets only; one primary direction; no direction repeats',
        stop='fixed calendar boundary; failures/missed consume slots; no optional stopping',
        end_exclusive=END, deadline_seconds=DEADLINE, repeat_workers=4,
        snapshot='exact stored 5c or 5b input; wait during 5b window; unchanged builder outside that window',
        actual='read-only candles.db; 13 closes for RV; final 5m close for target-hour close; saved reference close',
        runtime=sys.version, packages={p:package_version(p) for p in ('numpy','pandas','requests','openai','python-dotenv')},
        implementation_hashes={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in SOURCES})


def migrate(path):
    with db(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS phase6d_config (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), started_at TEXT NOT NULL,
          start_target INTEGER NOT NULL, end_exclusive INTEGER NOT NULL,
          config_json TEXT NOT NULL, config_hash TEXT NOT NULL, code_sha TEXT NOT NULL,
          source_db TEXT NOT NULL, claude_db TEXT NOT NULL, candles_db TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS phase6d_events (
          target INTEGER PRIMARY KEY CHECK(target%3600=0), status TEXT NOT NULL,
          started_at TEXT NOT NULL, completed_at TEXT, context TEXT, snapshot_hash TEXT,
          snapshot_source TEXT, reference_close REAL, gate TEXT NOT NULL DEFAULT 'unknown',
          single_class TEXT, repeat_modal TEXT, repeat_agreement REAL, active_votes INTEGER,
          reason TEXT, actual_rv REAL, actual_class TEXT, actual_return REAL,
          actual_direction TEXT, evaluated_at TEXT);
        CREATE TABLE IF NOT EXISTS phase6d_calls (
          target INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('class','direction')),
          slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 10), status TEXT NOT NULL,
          started_at TEXT NOT NULL, completed_at TEXT, api_started_at TEXT,
          class TEXT, p_quiet REAL, p_normal REAL, p_active REAL,
          direction TEXT, p_up REAL, p_down REAL, model_version TEXT, raw_json TEXT,
          error TEXT, correct INTEGER, brier REAL, active_brier REAL, signed_pnl REAL,
          PRIMARY KEY(target,kind,slot), CHECK(kind='class' OR slot=0));
        ''')


def checked(row, source, claude, candles):
    if row is None:
        raise RuntimeError('Explicit Phase 6d init required')
    cfg=json.loads(row['config_json'])
    if (digest(row['config_json']) != row['config_hash'] or cfg != specification(cfg['volatility_config'])
            or row['start_target']%3600 or row['start_target']>=END or row['end_exclusive']!=END
            or any(row[key]!=str(Path(value).resolve()) for key,value in
                   (('source_db',source),('claude_db',claude),('candles_db',candles)))):
        raise RuntimeError('Frozen Phase 6d config/source differs; restore registered code')
    return cfg


def initialize(path, source, claude, candles, clock=None):
    path,source,claude,candles=map(lambda p:Path(p).resolve(),(path,source,claude,candles))
    if path in (source,claude,candles):
        raise ValueError('Phase 6d requires its own database')
    clock=time.time() if clock is None else clock
    start=(int(clock)//3600+1)*3600
    migrate(path)
    with db(path) as c:
        prior=c.execute('SELECT * FROM phase6d_config').fetchone()
        if prior:
            checked(prior,source,claude,candles)
            return dict(prior)
    if start>=END:
        raise ValueError('Phase 6d calendar window has expired')
    with db(source,True) as c:
        original=c.execute('SELECT config_json,config_hash FROM phase6_config WHERE singleton=1').fetchone()
    if original is None:
        raise RuntimeError('Original frozen Phase 6 thresholds required')
    from phase4 import config_hash
    frozen=json.loads(original['config_json'])
    if config_hash(frozen)!=original['config_hash']:
        raise RuntimeError('Original Phase 6 manifest integrity failure')
    cfg=specification(frozen['gate']['volatility_config'])
    text=json.dumps(cfg,sort_keys=True)
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    dirty=subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,text=True)
    if dirty.strip():
        raise RuntimeError('Commit tracked changes before registering Phase 6d')
    with db(path) as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM phase6d_config').fetchone()
        if row:
            checked(row,source,claude,candles)
            return dict(row)
        c.execute('INSERT INTO phase6d_config VALUES(1,?,?,?,?,?,?,?,?,?)',
                  (stamp(),start,END,text,digest(text),sha,str(source),str(claude),str(candles)))
        return dict(c.execute('SELECT * FROM phase6d_config').fetchone())


def change(path, table, keys, **values):
    with db(path) as c:
        c.execute(f'UPDATE {table} SET '+','.join(k+'=?' for k in values)+' WHERE '+
                  ' AND '.join(k+'=?' for k in keys),tuple(values.values())+tuple(keys.values()))


def parse_class(raw):
    if raw.get('stop_reason')!='end_turn':
        raise ValueError('Incomplete class response')
    text=''.join(b['text'] for b in raw['content'] if b['type']=='text').strip()
    if text.startswith('```'):
        text='\n'.join(text.splitlines()[1:-1])
    value=json.loads(text)
    values=[value['p_'+label.lower()] for label in CLASSES]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) for v in values):
        raise ValueError('Class probabilities must be JSON numbers')
    p=probabilities(values)
    return dict(zip(('p_quiet','p_normal','p_active'),p), **{'class':winner(p)})


def invoke(path,target,kind,slot,context,cfg,clock_fn,call):
    keys=dict(target=target,kind=kind,slot=slot)
    with db(path) as c:
        claimed=c.execute('INSERT OR IGNORE INTO phase6d_calls(target,kind,slot,status,started_at) VALUES(?,?,?,\'started\',?)',
                          (target,kind,slot,stamp())).rowcount
    if not claimed:
        return None
    raw=None
    try:
        remaining=min(target+DEADLINE,END)-clock_fn()
        if remaining<=0 or clock_fn()<target:
            change(path,'phase6d_calls',keys,status='missed',completed_at=stamp(),error='Live deadline missed')
            return None
        if call is provider and not os.environ.get('ANTHROPIC_API_KEY'):
            raise RuntimeError('Credential missing')
        change(path,'phase6d_calls',keys,api_started_at=stamp())
        raw=call(kind,context,cfg,min(cfg['timeout'],remaining))
        result=parse_class(raw) if kind=='class' else parse_direction('direction',raw)
        ontime=target<=clock_fn()<min(target+DEADLINE,END)
        change(path,'phase6d_calls',keys,status='complete' if ontime else 'late',completed_at=stamp(),
               raw_json=json.dumps(raw),model_version=raw.get('model'),**result)
        return result if ontime else None
    except Exception as exc:
        change(path,'phase6d_calls',keys,status='failed',completed_at=stamp(),error=type(exc).__name__,
               raw_json=json.dumps(raw) if raw is not None else None)
        return None


def input_snapshot(source,claude,target):
    # No dependency on GPT's class/gate or on successful 5b/5c provider responses.
    try:
        with db(claude,True) as c:
            row=c.execute('SELECT context,snapshot_hash FROM phase5c_predictions WHERE target=?',(target,)).fetchone()
        if row and row['context']:
            if digest(row['context'])!=row['snapshot_hash']:
                raise ValueError('Saved Claude input hash mismatch')
            return row['context'],'phase5c'
    except (sqlite3.Error,OSError):
        pass
    with db(source,True) as c:
        registration=c.execute('SELECT start_target,end_target FROM phase5b_config WHERE singleton=1').fetchone()
        row=c.execute('SELECT context,snapshot_hash FROM phase5b_predictions WHERE target_candle_time=?',(target,)).fetchone()
    if row and row['context']:
        if digest(row['context'])!=row['snapshot_hash']:
            raise ValueError('Saved GPT input hash mismatch')
        return row['context'],'phase5b'
    if registration and registration['start_target']<=target<=registration['end_target']:
        return None,None
    return None,'builder'


def observe(path,source,claude,state,cfg,clock_fn,call,builder):
    clock=clock_fn(); target=int(clock)//3600*3600
    with db(path) as c:
        c.execute('BEGIN IMMEDIATE')
        # This worker owns the only 6d lock. A prior unfinished event is interrupted.
        c.execute("UPDATE phase6d_events SET status='interrupted',completed_at=?,reason='Interrupted reservation; no retry' WHERE status='started'",(stamp(),))
        for t in range(state['start_target'],min(target,END-3600)+1,3600):
            current=t==target and clock<t+DEADLINE and clock<END
            c.execute('INSERT OR IGNORE INTO phase6d_events(target,status,started_at,reason) VALUES(?,?,?,?)',
                      (t,'waiting' if current else 'missed',stamp(),None if current else 'Scheduling window missed'))
        c.execute("UPDATE phase6d_events SET status='missed',completed_at=?,reason='Input deadline missed' WHERE status='waiting' AND target+?<=?",(stamp(),DEADLINE,clock))
        event=c.execute('SELECT * FROM phase6d_events WHERE target=?',(target,)).fetchone()
    if not event or event['status']!='waiting' or clock>=END:
        return
    try:
        context,origin=input_snapshot(source,claude,target)
        if context is None and origin is None:
            return
        with db(path) as c:
            claimed=c.execute("UPDATE phase6d_events SET status='started' WHERE target=? AND status='waiting'",(target,)).rowcount
        if not claimed:
            return
        if origin=='builder':
            if builder is None:
                from phase4_runner import build_snapshot
                builder=build_snapshot
            context=builder(target)[0]
        features=aligned_features(context,target)
        change(path,'phase6d_events',{'target':target},context=context,snapshot_hash=digest(context),
               snapshot_source=origin,reference_close=features['reference'])
        single=invoke(path,target,'class',0,context,cfg,clock_fn,call)
        repeated=[]
        if single and single['class']=='ACTIVE':
            with ThreadPoolExecutor(max_workers=cfg['repeat_workers']) as pool:
                repeated=list(pool.map(lambda slot:invoke(path,target,'class',slot,context,cfg,clock_fn,call),range(1,11)))
        labels=[r['class'] if r else None for r in repeated]
        accepted,reason,active_votes=gate(single['class'] if single else None,labels)
        valid=single is not None and (single['class']!='ACTIVE' or len(labels)==10 and all(v in CLASSES for v in labels))
        if not target<=clock_fn()<min(target+DEADLINE,END):
            valid=accepted=False
            reason='Gate completed after deadline'
        modal=max(CLASSES,key=labels.count) if len(labels)==10 and all(v in CLASSES for v in labels) else None
        change(path,'phase6d_events',{'target':target},gate='accepted' if accepted else 'rejected' if valid else 'unknown',
               single_class=single['class'] if single else None,active_votes=active_votes,repeat_modal=modal,
               repeat_agreement=labels.count(modal)/10 if modal else None,reason=reason)
        if accepted:
            invoke(path,target,'direction',0,context,cfg,clock_fn,call)
        change(path,'phase6d_events',{'target':target},status='complete' if valid else 'failed',completed_at=stamp())
    except Exception as exc:
        change(path,'phase6d_events',{'target':target},status='failed',completed_at=stamp(),reason=type(exc).__name__)


def evaluate(path,candles,cfg,clock):
    from candle_store import read_window
    with db(path,True) as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM phase6d_events WHERE evaluated_at IS NULL AND target+3660<=?',(clock,))]
    for event in rows:
        target=event['target']; window=read_window(candles,target)
        try:
            rv=realized_volatility(list(window.values()),target)
        except ValueError:
            continue
        actual_class=classify(rv,cfg['volatility_config'])
        ret=100*(window[target+3300][4]/event['reference_close']-1) if event['reference_close'] else None
        actual_direction=None if ret is None else 'UP' if ret>0 else 'DOWN' if ret<0 else 'FLAT'
        with db(path) as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT evaluated_at FROM phase6d_events WHERE target=?',(target,)).fetchone()[0]:
                continue
            c.execute('UPDATE phase6d_events SET actual_rv=?,actual_class=?,actual_return=?,actual_direction=?,evaluated_at=? WHERE target=?',
                      (rv,actual_class,ret,actual_direction,stamp(),target))
            for row in c.execute("SELECT * FROM phase6d_calls WHERE target=? AND status='complete'",(target,)).fetchall():
                if row['kind']=='class':
                    correct=int(row['class']==actual_class)
                    score=brier([row['p_quiet'],row['p_normal'],row['p_active']],actual_class)
                    active_score=(row['p_active']-int(actual_class=='ACTIVE'))**2
                    pnl=None
                else:
                    correct=None if actual_direction in (None,'FLAT') else int(row['direction']==actual_direction)
                    score=None if actual_direction in (None,'FLAT') else (row['p_up']-int(actual_direction=='UP'))**2
                    active_score=None
                    pnl=None if ret is None else ret*(1 if row['direction']=='UP' else -1)
                c.execute('UPDATE phase6d_calls SET correct=?,brier=?,active_brier=?,signed_pnl=? WHERE target=? AND kind=? AND slot=?',
                          (correct,score,active_score,pnl,target,row['kind'],row['slot']))


def cycle(path,source,claude,candles,clock_fn=time.time,call=provider,builder=None):
    with db(path,True) as c:
        state=c.execute('SELECT * FROM phase6d_config WHERE singleton=1').fetchone()
        cfg=checked(state,source,claude,candles); state=dict(state)
    with open(str(path)+'.lock','a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            observe(path,source,claude,state,cfg,clock_fn,call,builder)
        finally:
            evaluate(path,candles,cfg,clock_fn())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('init','cycle','status'))
    p.add_argument('--db',type=Path,default=ROOT/'phase6d.db')
    p.add_argument('--source-db',type=Path,default=ROOT/'btc.db')
    p.add_argument('--claude-db',type=Path,default=ROOT/'claude-observation.db')
    p.add_argument('--candles-db',type=Path,default=ROOT/'candles.db')
    args=p.parse_args()
    if args.action=='init':
        os.umask(0o077)
        print(json.dumps(initialize(args.db,args.source_db,args.claude_db,args.candles_db),indent=2))
    elif args.action=='cycle':
        cycle(args.db,args.source_db,args.claude_db,args.candles_db)
    else:
        with db(args.db,True) as c:
            print(dict(c.execute('SELECT start_target,end_exclusive,config_hash,code_sha FROM phase6d_config').fetchone()))
            print('Events:',[dict(r) for r in c.execute('SELECT status,gate,count(*) AS n FROM phase6d_events GROUP BY status,gate')])
            print('Calls:',[dict(r) for r in c.execute('SELECT kind,status,count(*) AS n,sum(api_started_at IS NOT NULL) AS attempts FROM phase6d_calls GROUP BY kind,status')])


if __name__=='__main__': main()
