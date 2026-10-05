"""Read-only Phase 6 reporting. No provider calls, migrations or training."""
import itertools
import json
import math
import sqlite3
import statistics
from pathlib import Path
from phase6_methods import METHODS
from ui import card, metric_blocks, page_shell, records_table, styled_table


def mean(values):
    return statistics.mean(values) if values else None


def paired(a, b):
    common = sorted(set(a)&set(b))
    left = sum(a[t]['correct']==1 and b[t]['correct']==0 for t in common)
    right = sum(a[t]['correct']==0 and b[t]['correct']==1 for t in common)
    discordant = left+right
    p = min(1., 2*sum(math.comb(discordant,k) for k in range(min(left,right)+1))/2**discordant) if discordant else 1.
    probability_common = [t for t in common if a[t]['brier'] is not None and b[t]['brier'] is not None]
    return dict(common_n=len(common), left_only_correct=left, right_only_correct=right,
                accuracy_difference=mean([a[t]['correct']-b[t]['correct'] for t in common]),
                brier_common_n=len(probability_common),
                brier_difference=mean([a[t]['brier']-b[t]['brier'] for t in probability_common]),
                pnl_difference=mean([a[t]['signed_pnl']-b[t]['signed_pnl'] for t in common]),
                exact_mcnemar_p=p if len(common)>=20 else None)


def report(path):
    result = dict(status='Not started', config=None, config_hash=None, code_sha=None,
                  accepted=0, rejected=0, pending=0, all_hours=0, recorded_hours=0,
                  missing_hours=0, single_active=0, scored_events=0, methods=[], paired=[],
                  recent=[], recent_accepted=[], recent_predictions=[], missing=[])
    try:
        with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
            c.row_factory = sqlite3.Row
            c.execute('BEGIN')
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'phase6_config' not in tables:
                return result
            state = c.execute('SELECT * FROM phase6_config').fetchone()
            if state is None:
                return result
            events = [dict(r) for r in c.execute('SELECT * FROM phase6_events ORDER BY target')]
            predictions = [dict(r) for r in c.execute('''SELECT p.*,e.actual_return,e.actual_direction FROM phase6_predictions p
                JOIN phase6_events e USING(target) WHERE e.gate='accepted' ORDER BY p.target,p.method''')]
    except sqlite3.OperationalError:
        result['status'] = 'Phase 6 database unavailable'
        return result
    cfg = json.loads(state['config_json'])
    accepted = [e for e in events if e['gate']=='accepted']
    # Scheduled hours through the last recorded event include absent scheduler cycles.
    import time
    end = max(e['target'] for e in accepted) if len(accepted)>=cfg['stop']['accepted_gates'] else int(time.time())//3600*3600
    all_hours = max(0,(end-state['start_target'])//3600+1)
    all_hours = max(all_hours,len(events))
    single_active = sum(e['single_class']=='ACTIVE' for e in events)
    result.update(status='Stopped: fixed accepted-gate limit' if len(accepted)>=96 else 'Prospective experiment running',
        config=cfg, config_hash=state['config_hash'], code_sha=state['code_sha'], started_at=state['started_at'],
        start_target=state['start_target'], accepted=len(accepted), rejected=sum(e['gate']=='rejected' for e in events),
        pending=sum(e['gate']=='pending' for e in events), all_hours=all_hours, recorded_hours=len(events),
        missing_hours=max(0,all_hours-len(events)), single_active=single_active,
        scored_events=sum(e['actual_direction'] in ('UP','DOWN') for e in accepted),
        recent_accepted=[{k:e[k] for k in ('target','single_class','active_votes','actual_return','actual_direction')} for e in accepted[-12:][::-1]],
        recent_predictions=[{k:p[k] for k in ('target','method','status','direction','p_up','p_down','version','model_version','correct','brier','signed_pnl','reason')} for p in predictions if p['target'] in {e['target'] for e in accepted[-12:]}],
        recent=[{k:e[k] for k in ('target','gate','single_class','active_votes','reason','actual_return','actual_direction')} for e in events[-24:][::-1]],
        missing=[{k:p[k] for k in ('target','method','status','reason')} for p in predictions if p['status']!='complete'])
    indexes = {}
    for method in METHODS:
        available = [p for p in predictions if p['method']==method and p['status']=='complete']
        scored = [p for p in available if p['correct'] is not None]
        pnl = [p['signed_pnl'] for p in available if p['signed_pnl'] is not None]
        absolute = [abs(p['actual_return']) for p in available if p['actual_return'] is not None]
        briers = [p['brier'] for p in scored if p['brier'] is not None]
        correct = sum(p['correct'] for p in scored)
        result['methods'].append(dict(method=method, n=len(scored), correct=correct,
            accuracy=correct/len(scored) if scored else None, brier_n=len(briers), brier=mean(briers),
            pnl_n=len(pnl), mean_signed_pnl=mean(pnl), mean_absolute_return=mean(absolute),
            median_absolute_return=statistics.median(absolute) if absolute else None,
            available=len(available), coverage_all=len(available)/all_hours if all_hours else None,
            coverage_active=len(available)/single_active if single_active else None,
            coverage_accepted=len(available)/len(accepted) if accepted else None))
        indexes[method] = {p['target']:p for p in scored}
    for a,b in itertools.combinations(METHODS,2):
        comparison = paired(indexes[a],indexes[b])
        if comparison['common_n']:
            result['paired'].append(dict(left=a,right=b,**comparison))
    return result


def page(path, analysis=False):
    data = report(path)
    introduction = ('<h1>Active Direction — Phase 6</h1>'
        '<p>Prospective high-confidence-ACTIVE direction prediction. '
        'Fixed gate: single-shot ACTIVE + ten valid repeated classifications, modal ACTIVE, '
        'ACTIVE votes ≥8/10. Effective sample count is accepted distinct target hours, not API calls. '
        'Stop after 96 accepted gates, including gates with missing direction outputs.</p>'
        '<p>Phase 1–3 retrospective ACTIVE accuracy was approximately 34.9% for OpenAI and 41.9% for Jev. '
        'ACTIVE is not assumed easier: this experiment compares alternative conditional signals prospectively.</p>')
    counts = card(data['status'], metric_blocks([('Accepted / 96',str(data['accepted'])+' / 96'),
        ('Rejected / NO SIGNAL',data['rejected']),('Pending',data['pending']),('Scored ACTIVE events',data['scored_events']),
        ('All scheduled hours',data['all_hours']),('Missing hours',data['missing_hours']),('Single-shot ACTIVE hours',data['single_active'])]))
    def fmt(v):
        return '—' if v is None else f'{v:.5f}' if isinstance(v,float) else str(v)
    fields = ('method','n','correct','accuracy','brier_n','brier','pnl_n','mean_signed_pnl',
              'mean_absolute_return','median_absolute_return','coverage_all','coverage_active','coverage_accepted')
    scoreboard = card('Method scoreboard · including ensembles', styled_table(fields,
        [[fmt(m[k]) for k in fields] for m in data['methods']]))
    explanation = ('<p>Accuracy/coverage are fractions. N excludes FLAT; Brier only for probability methods. '
        'PnL = ± target percent return for unit long/short, no fees/spread. FLAT contributes zero PnL. '
        'Coverage uses available predictions, all scheduled hours (missing cycles included), observed single-shot ACTIVE '
        'hours and accepted gates. Rule abstentions and provider failures remain missing; rules have no invented probabilities. '
        'Probability ensemble needs two eligible probability methods; majority vote needs three methods and abstains on ties. '
        'Missing-hour ACTIVE status is unknown.</p>')
    content = introduction+counts+scoreboard+explanation
    if analysis:
        content += card('Paired comparisons on common events',
            '<p>Two-sided exact McNemar p-values appear from 20 common scored events. '
            'Exploratory comparisons are unadjusted for multiple testing; differences are left minus right. '
            'Brier differences use the probability intersection only.</p>'+records_table(data['paired']))
    content += card('Recent accepted ACTIVE events',records_table(data['recent_accepted']))
    content += card('Recent accepted-event method predictions',records_table(data['recent_predictions']))
    content += card('Recent hours · accepted and rejected',records_table(data['recent']))
    content += card('Missing method outputs · never backfilled',records_table(data['missing'][-60:]))
    if data['config']:
        from html import escape
        content += card('Frozen preregistration', '<p>Started: '+escape(data['started_at'])+' · Code: '+escape(data['code_sha'])+
            ' · Config hash: '+escape(data['config_hash'])+'</p><details><summary>Frozen design</summary><pre>'+
            escape(json.dumps(data['config'],indent=2,sort_keys=True))+'</pre></details>')
    return page_shell('Active Direction — Phase 6',content,'analysis' if analysis else 'phase6',
                      phase='phase6' if analysis else None,refresh=300)
