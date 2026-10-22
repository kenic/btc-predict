"""Read-only Phase 6d ACTIVE detection, independent gate and direction analysis."""
import argparse
from collections import Counter
import json
import math
import sqlite3
from pathlib import Path

from claude_observation import db, digest, has_table
from claude_analysis import metrics, mean
from phase6d import ROOT
from phase6 import gate
from volatility import CLASSES


def detection(rows):
    # Each row: predicted ACTIVE?, actual ACTIVE?, optional p_active.
    tp=sum(p and a for p,a,_ in rows); fn=sum(not p and a for p,a,_ in rows)
    fp=sum(p and not a for p,a,_ in rows); tn=sum(not p and not a for p,a,_ in rows)
    scored=[(p-a)**2 for _,a,p in rows if p is not None]
    return dict(n=len(rows),tp=tp,fn=fn,fp=fp,tn=tn,
        actual_active_n=tp+fn,predicted_active_n=tp+fp,
        recall=tp/(tp+fn) if tp+fn else None,precision=tp/(tp+fp) if tp+fp else None,
        accuracy=(tp+tn)/len(rows) if rows else None, brier_n=len(scored),brier=mean(scored))


def matches(a,b):
    return a['target']==b['target'] and a['snapshot_hash']==b['snapshot_hash'] and a['context']==b['context']


def same_outcome(a,b):
    return (a['actual_direction']==b['actual_direction'] and a['actual_return'] is not None
            and b['actual_return'] is not None and math.isclose(a['actual_return'],b['actual_return'],abs_tol=1e-10,rel_tol=0))


def report(path,source=None,claude=None):
    with db(path,True) as c:
        c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        state=dict(c.execute('SELECT * FROM phase6d_config WHERE singleton=1').fetchone())
        cfg=json.loads(state['config_json'])
        events={r['target']:dict(r) for r in c.execute('SELECT * FROM phase6d_events ORDER BY target')}
        calls=[dict(r) for r in c.execute('''SELECT target,kind,slot,status,class,p_quiet,p_normal,p_active,
            direction,p_up,p_down,correct,brier,active_brier,signed_pnl FROM phase6d_calls ORDER BY target,kind,slot''')]
    source=Path(source or state['source_db']);claude=Path(claude or state['claude_db'])
    singles={r['target']:r for r in calls if r['kind']=='class' and r['slot']==0 and r['status']=='complete'}
    directions={r['target']:r for r in calls if r['kind']=='direction' and r['status']=='complete' and r['signed_pnl'] is not None}
    labels={t:e['actual_class']=='ACTIVE' for t,e in events.items() if e['actual_class'] in CLASSES}
    detection_rows=[(r['class']=='ACTIVE',labels[t],r['p_active']) for t,r in singles.items() if t in labels]
    gate_rows=[(e['gate']=='accepted',labels[t],None) for t,e in events.items()
               if t in labels and e['status']=='complete' and e['gate'] in ('accepted','rejected')]
    source_ok=True; original={};method_maps={'claude6d':directions,'gpt':{},'logistic':{},'claude6c':{}}
    try:
        with db(source,True) as c:
            for cohort in ('phase6','phase6b'):
                if not has_table(c,cohort+'_events'):continue
                for row in c.execute(f'SELECT * FROM {cohort}_events ORDER BY target'):
                    e=dict(row);t=e['target']
                    if t not in events or not matches(events[t],e):continue
                    original[t]=dict(e,cohort=cohort,p_active=None)
                    if has_table(c,cohort+'_calls'):
                        call=c.execute(f'SELECT probabilities_json FROM {cohort}_calls WHERE target=? AND slot=0 AND status=\'complete\'',(t,)).fetchone()
                        if call and call[0]:original[t]['p_active']=json.loads(call[0])[2]
                    if has_table(c,cohort+'_predictions'):
                        for pred in c.execute(f'SELECT * FROM {cohort}_predictions WHERE target=? AND method IN (\'gpt\',\'logistic\') AND status=\'complete\' AND signed_pnl IS NOT NULL',(t,)):
                            if same_outcome(events[t],e):method_maps[pred['method']][t]=dict(pred)
    except (sqlite3.Error,OSError):
        source_ok=False
    class_common=[t for t in singles if t in labels and t in original and original[t]['single_class'] in CLASSES]
    class_comparison={
        'claude':detection([(singles[t]['class']=='ACTIVE',labels[t],singles[t]['p_active']) for t in class_common]),
        'gpt':detection([(original[t]['single_class']=='ACTIVE',labels[t],original[t]['p_active']) for t in class_common])}
    overlaps=Counter()
    for t,e in events.items():
        if e['status']!='complete' or e['gate'] not in ('accepted','rejected') or t not in original:continue
        g=original[t]
        metadata=json.loads(g['gate_json']) if g['gate_json'] else {}
        repeated=metadata.get('repeats',[])
        if g['single_class'] not in CLASSES or (g['single_class']=='ACTIVE' and
            (len(repeated)!=10 or any(v not in CLASSES for v in repeated))):continue
        expected=gate(g['single_class'],repeated)[0]
        if g['gate'] not in ('accepted','rejected') or expected!=(g['gate']=='accepted'):continue
        overlaps[('both_accepted' if expected else 'claude_only_accepted') if e['gate']=='accepted'
                 else ('gpt_only_accepted' if expected else 'neither_accepted')]+=1
    claude_ok=True
    try:
        with db(claude,True) as c:
            for row in c.execute('''SELECT p.*,e.context,e.snapshot_hash,e.actual_return,e.actual_direction
                FROM phase6c_calls p JOIN phase6c_events e ON e.target=p.target AND e.cohort=p.cohort
                WHERE p.slot=0 AND p.status='complete' AND p.signed_pnl IS NOT NULL'''):
                r=dict(row);t=r['target']
                if t in events and matches(events[t],r) and same_outcome(events[t],r):
                    method_maps['claude6c'][t]=r
    except (sqlite3.Error,OSError):claude_ok=False
    common=set.intersection(*(set(m) for m in method_maps.values()))
    direction_comparison=dict(common_target_n=len(common),methods={name:metrics([rows[t] for t in sorted(common)])
                                                                 for name,rows in method_maps.items()})
    pair=set(directions)&set(method_maps['gpt'])
    same_different={}
    for label in ('same','different'):
        targets=[t for t in sorted(pair) if (directions[t]['direction']==method_maps['gpt'][t]['direction'])==(label=='same')]
        same_different[label]=dict(n=len(targets),claude6d=metrics([directions[t] for t in targets]),
                                  gpt=metrics([method_maps['gpt'][t] for t in targets]))
    return dict(version=cfg['version'],start_target=state['start_target'],end_exclusive=state['end_exclusive'],
        model=cfg['params']['model'],params=cfg['params'],
        thresholds={k:cfg['volatility_config'][k] for k in ('quiet_upper','active_lower')},config_hash=state['config_hash'],exploratory=True,
        recorded=len(events),planned=(state['end_exclusive']-state['start_target'])//3600,
        statuses=dict(Counter(e['status'] for e in events.values())),gates=dict(Counter(e['gate'] for e in events.values())),
        class_calls=dict(Counter(r['status'] for r in calls if r['kind']=='class')),
        direction_calls=dict(Counter(r['status'] for r in calls if r['kind']=='direction')),
        actuals_available=len(labels),actuals_pending=len(events)-len(labels),
        active_detection=detection(detection_rows),gate_detection=detection(gate_rows),
        class_accuracy=mean([singles[t]['correct'] for t in singles if t in labels]),
        class_brier=mean([singles[t]['brier'] for t in singles if t in labels]),
        direction=metrics(list(directions.values())),class_common_target_n=len(class_common),
        class_comparison=class_comparison,gate_overlap=dict(overlaps),gate_common_target_n=sum(overlaps.values()),
        common_direction=direction_comparison,same_different=same_different,
        comparison_available=source_ok,claude6c_comparison_available=claude_ok,
        recent=[{key:e[key] for key in ('target','status','gate','single_class','repeat_modal','repeat_agreement',
                 'active_votes','reason','actual_rv','actual_class','actual_return','actual_direction')}
                 | {'direction_status':next((r['status'] for r in calls if r['target']==e['target'] and r['kind']=='direction'),'not_requested')}
                 | {key:(next((r[key] for r in calls if r['target']==e['target'] and r['kind']=='direction'),None))
                    for key in ('direction','p_up','p_down','correct','brier','signed_pnl')}
                 for e in reversed(list(events.values())[-100:])])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,default=ROOT/'phase6d.db')
    args=p.parse_args()
    print(json.dumps(report(args.db),indent=2,allow_nan=False))


if __name__=='__main__':main()
