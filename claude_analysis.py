"""Read-only timestamp/snapshot matched exploratory Claude comparison reports."""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
from claude_observation import ROOT, db, has_table


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values)/len(values) if values else None


def metrics(rows):
    return dict(n=len(rows), accuracy_n=sum(r['correct'] is not None for r in rows),
                accuracy=mean([r['correct'] for r in rows]),
                brier_n=sum(r['brier'] is not None for r in rows), brier=mean([r['brier'] for r in rows]),
                mean_signed_pnl=mean([r['signed_pnl'] for r in rows]),
                total_signed_pnl=sum(r['signed_pnl'] for r in rows) if rows else None)


def correlation(x, y):
    if len(x) < 2:
        return None
    mx, my = mean(x), mean(y)
    xx, yy = sum((v-mx)**2 for v in x), sum((v-my)**2 for v in y)
    return sum((a-mx)*(b-my) for a,b in zip(x,y))/math.sqrt(xx*yy) if xx and yy else None


def rv_metrics(rows, key):
    pred, actual = [r[key] for r in rows], [r['actual_rv'] for r in rows]
    return dict(n=len(rows), mae=mean([abs(p-a) for p,a in zip(pred,actual)]),
                bias=mean([p-a for p,a in zip(pred,actual)]), correlation=correlation(pred,actual))


def report(side, source):
    with db(side, True) as c:
        # Explicit timestamp JOIN; hash equality enforces exactly matched saved input.
        c.execute('ATTACH DATABASE ? AS original', (Path(source).resolve().as_uri()+'?mode=ro',))
        rv = [dict(r) for r in c.execute('''SELECT a.target,a.actual_rv,a.predicted_rv AS claude,
            b.predicted_rv AS gpt FROM phase5c_predictions a JOIN original.phase5b_predictions b
            ON b.target_candle_time=a.target AND b.snapshot_hash=a.snapshot_hash AND b.context=a.context
            WHERE a.status='complete' AND b.status='complete' AND a.actual_rv IS NOT NULL
            AND b.actual_rv IS NOT NULL AND abs(a.actual_rv-b.actual_rv)<1e-12''')]
        state = dict(c.execute('SELECT start_target,end_exclusive,config_hash FROM claude_config').fetchone())
        calls = [dict(r) for r in c.execute('SELECT * FROM phase6c_calls')]
        events = {(r['cohort'], r['target']): dict(r) for r in c.execute('SELECT * FROM phase6c_events')}
        coverage = {t: [dict(r) for r in c.execute(f'SELECT status,count(*) AS n FROM {t} GROUP BY status')]
                    for t in ('phase5c_predictions','phase6c_calls')}
    primary = {(r['cohort'],r['target']): r for r in calls if r['slot']==0 and r['status']=='complete' and r['signed_pnl'] is not None}
    comparable = {}
    with db(source, True) as c:
        for cohort in ('phase6', 'phase6b'):
            if not has_table(c, cohort+'_predictions'):
                continue
            for r in c.execute(f'''SELECT p.*,e.snapshot_hash,e.context FROM {cohort}_predictions p
                JOIN {cohort}_events e ON e.target=p.target WHERE p.method IN ('gpt','logistic')
                AND p.status='complete' AND p.signed_pnl IS NOT NULL AND e.gate='accepted' '''):
                key = cohort, r['target']
                if key in primary and events[key]['snapshot_hash'] == r['snapshot_hash'] and events[key]['context'] == r['context']:
                    comparable.setdefault(key, {'claude': primary[key]})[r['method']] = dict(r)
    common = {k:v for k,v in comparable.items() if len(v)==3}
    def comparison(items):
        return dict(common_target_n=len(items), methods={m: metrics([r[m] for r in items.values()])
                                                       for m in ('gpt','logistic','claude')})
    repeats = defaultdict(list)
    for r in calls:
        if r['slot']>0 and r['status']=='complete':
            repeats[r['cohort'],r['target']].append(r)
    grouped = defaultdict(list)
    repeat_cases = []
    for key,event in sorted(events.items()):
        rr = repeats[key]
        votes = Counter(r['direction'] for r in rr)
        full = len(rr)==10
        modal = ('UP' if votes['UP']>votes['DOWN'] else 'DOWN') if full and votes['UP']!=votes['DOWN'] else None
        agreement = max(votes.values(), default=0)/10 if full else None
        case = dict(cohort=key[0],target=key[1],successful_repeats=len(rr),modal_direction=modal,
                    agreement=agreement,up_votes=votes['UP'],down_votes=votes['DOWN'],
                    tie=full and votes['UP']==votes['DOWN'])
        repeat_cases.append(case)
        if full and key in primary:
            grouped[str(agreement)].append((primary[key], event, modal))
    repeat_performance = {}
    for agreement, rows in sorted(grouped.items()):
        modal_rows = [dict(correct=None if e['actual_direction']=='FLAT' else int(m==e['actual_direction']),
                           brier=None, signed_pnl=e['actual_return']*(1 if m=='UP' else -1))
                      for _, e, m in rows if m is not None]
        repeat_performance[agreement] = dict(primary=metrics([p for p,_,_ in rows]), modal=metrics(modal_rows))
    pairs = {k:v for k,v in comparable.items() if 'gpt' in v}
    same_different = {}
    cases = []
    for label in ('same','different'):
        subset = {k:v for k,v in pairs.items() if (v['gpt']['direction']==v['claude']['direction']) == (label=='same')}
        same_different[label] = dict(n=len(subset), **{m:metrics([v[m] for v in subset.values()]) for m in ('gpt','claude')})
        cases.extend(dict(cohort=k[0],target=k[1],case=label,actual=events[k]['actual_direction'],
                          gpt_direction=v['gpt']['direction'],claude_direction=v['claude']['direction'],
                          gpt_p_up=v['gpt']['p_up'],claude_p_up=v['claude']['p_up'],
                          gpt_signed_pnl=v['gpt']['signed_pnl'],claude_signed_pnl=v['claude']['signed_pnl']) for k,v in subset.items())
    return dict(manifest=state, exploratory=True, coverage=coverage,
                rv={'gpt':rv_metrics(rv,'gpt'),'claude':rv_metrics(rv,'claude'),
                    'gpt_claude_correlation':correlation([r['gpt'] for r in rv],[r['claude'] for r in rv])},
                direction=comparison(common),
                by_cohort={name:comparison({k:v for k,v in common.items() if k[0]==name}) for name in ('phase6','phase6b')},
                repeat_agreement_performance=repeat_performance,repeat_cases=repeat_cases,
                same_different=same_different,same_different_cases=sorted(cases,key=lambda x:(x['target'],x['cohort'])))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,default=ROOT/'claude-observation.db')
    p.add_argument('--source-db',type=Path,default=ROOT/'btc.db')
    args=p.parse_args()
    print(json.dumps(report(args.db,args.source_db),indent=2,allow_nan=False))


if __name__=='__main__':
    main()
