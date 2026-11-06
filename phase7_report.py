"""Read-only Phase 7 report. No provider calls or database creation."""
from collections import Counter
import json
import math
from pathlib import Path
import random
import sqlite3
import statistics
import time
from phase7_model import quote_pnl

METHODS=('selector','gpt','order_flow','logistic','ensemble_vote','ensemble_probability','train_best_fixed','cash')
LABELS={'selector':'Selector','gpt':'GPT','order_flow':'Order Flow','logistic':'Frozen Logistic Regression',
        'ensemble_vote':'Majority ensemble','ensemble_probability':'Probability ensemble','train_best_fixed':'Training-selected fixed','cash':'Cash'}

def direction(e,m):
    p=json.loads(e['predictions_json'] or '{}').get(m,{})
    return p.get('direction') if p.get('status')=='complete' else None

def metrics(values,trades):
    total=peak=dd=0
    for v in values:
        total+=v;peak=max(peak,total);dd=max(dd,peak-total)
    return dict(n=len(values),trades=trades,mean_net_bps=statistics.mean(values) if values else None,
                sum_net_bps=sum(values),max_drawdown_bps=dd)

def block_ci(daily,block=3,replicates=10000):
    if len(daily)<2:return None
    rng=random.Random(72026);out=[];n=len(daily)
    for _ in range(replicates):
        sample=[]
        while len(sample)<n:
            start=rng.randrange(n);sample.extend(daily[(start+j)%n] for j in range(block))
        sample=sample[:n];count=sum(v[1] for v in sample)
        if count:out.append(sum(v[0] for v in sample)/count)
    if not out:return None
    out.sort();return [out[int(.025*(len(out)-1))],out[int(.975*(len(out)-1))]]

def report(path,clock=None,final_ci=True):
    clock=time.time() if clock is None else clock
    if not Path(path).exists():return dict(status='Not armed')
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    try:
        c.execute('BEGIN');state=c.execute('SELECT * FROM config').fetchone()
        if not state:return dict(status='Not armed')
        a=json.loads(state['arm_json']);f=json.loads(state['frozen_json']) if state['frozen_json'] else None
        ee=[dict(e) for e in c.execute('SELECT * FROM events ORDER BY target')]
        cc=Counter(r[0] for r in c.execute('SELECT status FROM calls'))
    finally:c.close()
    out=dict(status='Waiting for final Phase 6b outcome',armed_at=state['armed_at'],days=60,arm_hash=state['arm_hash'],
             model_hash=state['frozen_hash'],code_sha=a['code_sha'],planned_hours=1440,recorded_hours=len(ee),
             start_target=state['start_target'],end_exclusive=state['end_exclusive'],calls=dict(cc),
             training=None,methods={},cost_sensitivity={},recent=[],primary=None)
    if not f:return out
    out['training']={k:f[k] for k in ('training_n','selector','train_best_fixed','training_snapshot_hash','fit_cutoff')}
    out['training']['logistic_n']=f['logistic']['n']
    end=state['end_exclusive'];start=state['start_target']
    out['status']='Scheduled' if clock<start else 'Running' if clock<end else 'Observation ended; final evaluation'
    due=max(0,min(1440,int((clock-start-600)//3600)+1))
    eligible_hours=[e for e in ee if e['target']+600<clock]
    out['due_hours']=due;out['missing_hours']=max(0,due-len(eligible_hours))
    out['gate_counts']=dict(Counter(e['gate'] or 'unknown' for e in ee))
    out['event_statuses']=dict(Counter(e['status'] for e in ee))
    accepted=[e for e in ee if e['gate']=='accepted']
    completed=[e for e in accepted if e['execution_status']=='complete']
    out['accepted']=len(accepted);out['execution_counts']=dict(Counter(e['execution_status'] or 'unknown' for e in accepted))
    out['executable_n']=len(completed)
    out['prediction_coverage']={m:dict(available=sum(direction(e,m) is not None for e in accepted),accepted=len(accepted)) for m in METHODS}
    out['provisional']=clock<end+60 or len(completed)!=len(accepted)
    values={m:[] for m in METHODS}
    for cost in (0,2,5,10,20):
        out['cost_sensitivity'][str(cost)]={}
        for m in METHODS:
            vv=[quote_pnl(direction(e,m),json.loads(e['entry_json']),json.loads(e['exit_json']),cost) for e in completed]
            out['cost_sensitivity'][str(cost)][m]=metrics(vv,sum(direction(e,m) is not None for e in completed))
            if cost==5:values[m]=vv;out['methods'][m]=out['cost_sensitivity']['5'][m]
    for m in METHODS:
        scored=[e for e in accepted if e['actual_direction'] in ('UP','DOWN') and direction(e,m)]
        out['methods'][m]['direction_n']=len(scored)
        out['methods'][m]['direction_accuracy']=statistics.mean(direction(e,m)==e['actual_direction'] for e in scored) if scored else None
    diffs=[a-b for a,b in zip(values['selector'],values['order_flow'])]
    out['primary']=dict(mean_delta_bps=statistics.mean(diffs) if diffs else None,ci95=None,verdict='Not tested before fixed end')
    if clock>=end+60 and final_ci:
        days=list(range(start//86400,(end-1)//86400+1));daily={d:[0.0,0] for d in days}
        for e,v in zip(completed,diffs):daily[e['target']//86400][0]+=v;daily[e['target']//86400][1]+=1
        ci=block_ci(list(daily.values()));out['primary']['ci95']=ci
        out['primary']['sensitivity_ci']={str(b):block_ci(list(daily.values()),b) for b in (1,7)}
        known_gates=sum(e['gate'] in ('accepted','rejected') and e['status']=='complete' for e in ee)
        sufficient=(len(completed)>=100 and sum(v[1]>0 for v in daily.values())>=30 and known_gates/1440>=.95
                    and len(completed)==len(accepted))
        out['primary']['verdict']=('Insufficient coverage or unresolved outcomes' if not sufficient else
            'Supports preregistered improvement' if ci and ci[0]>0 and statistics.mean(diffs)>=1 else 'Improvement not established')
    out['recent']=[{k:e[k] for k in ('target','status','gate','selection','ready_at','execution_status','actual_return')} for e in reversed(ee[-48:])]
    return out

def render(path):
    from ui import page_shell,card,metric_blocks,styled_table
    from datetime import datetime,timezone
    from html import escape
    r=report(path);content='<h1>Phase 7 · Frozen model selector</h1><p>'+escape(r['status'])+'</p>'
    content+='<p>60 days · Paper execution only · Bid/ask spread plus 5 bp round-trip cost. No live trading.</p>'
    if r.get('start_target'):
        fmt=lambda t:datetime.fromtimestamp(t,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        content+=card('Fixed window','<p>'+fmt(r['start_target'])+' → '+fmt(r['end_exclusive'])+' (exclusive)</p>')
        content+=card('Coverage',metric_blocks([('Recorded hours',r['recorded_hours']),('Accepted',r['accepted']),('Executable',r['executable_n']),('Missing hours',r['missing_hours'])]))
        train=r['training'];content+=card('Frozen selection rule','<pre>'+escape(json.dumps(train['selector'],indent=2))+'</pre><p>Training: 200 accepted Phase 6 + 6b targets. Logistic fit: '+str(train['logistic_n'])+' labeled snapshots.</p>')
        rows=[]
        for m,v in r['methods'].items():rows.append([LABELS[m],v['n'],v['trades'],'—' if v['mean_net_bps'] is None else f"{v['mean_net_bps']:.3f}",f"{v['max_drawdown_bps']:.2f}"])
        content+=card('Provisional paper results',styled_table(('Method','Opportunities','Trades','Mean net bp','Max DD bp'),rows))
        content+=card('Primary comparison','<p>'+escape(r['primary']['verdict'])+'</p><p>Selector − Order Flow. Confidence interval evaluated only after the fixed end. Missing execution outcomes prevent a positive final verdict.</p>')
        content+=card('Recent hours',styled_table(('Target UTC','Gate','Selection','Execution'),[[fmt(e['target']),e['gate'],e['selection'],e['execution_status']] for e in r['recent']]))
    content+='<p><a href="/phase7/data.json">Full report JSON</a></p>'
    return page_shell('Phase 7',content,'phase7',refresh=60)
