"""Read-only Phase 6 reporting. No provider calls, migrations or training."""
import itertools
import json
import math
import sqlite3
import statistics
import time
from pathlib import Path
from phase6_methods import BASELINES, DIRECTION_METHODS, DIRECTION_ENSEMBLES, METHODS
from volatility import CLASSES, classify
from direction_volatility import load_candles, assign_rv
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


def gate_summary(event):
    """Presentation of saved repeat results, never a new gate evaluation."""
    metadata = json.loads(event['gate_json']) if event.get('gate_json') else {}
    repeated = metadata.get('repeats', [])
    valid = len(repeated) == 10 and all(label in CLASSES for label in repeated)
    modal = max(CLASSES, key=repeated.count) if valid else None
    return dict(repeat_modal_class=modal,
                repeat_agreement=repeated.count(modal)/10 if valid else None)


def actual_volatility(events, connection, tables, config, clock):
    """Read-only visibility using saved Phase 4/5 outcomes or exact cached 5m data.

    No network fetching, DB writes, interpolation or new RV definition. Cached
    candles use the existing Phase 4 calculation via assign_rv. Conflicting saved
    outcomes or a cache inconsistent with the saved target close stay unavailable.
    """
    saved = {}
    for table, target in (('phase5_predictions','target_candle_time'),
                          ('volatility_predictions','target_candle_time')):
        if table not in tables:
            continue
        for row in connection.execute(f'SELECT {target},actual_rv FROM {table} WHERE actual_rv IS NOT NULL'):
            saved.setdefault(row[0],[]).append(row[1])
    outcomes, missing = {}, []
    for event in events:
        t = event['target']
        outcomes[t] = dict(actual_volatility=None, actual_regime=None,
                           actual_volatility_status='pending' if clock<t+3600 else 'unavailable')
        if clock<t+3600:
            continue
        values = saved.get(t,[])
        if values:
            if any(not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in values) or len(set(values))!=1:
                continue
            rv = values[0]
            outcomes[t].update(actual_volatility=rv, actual_regime=classify(rv,config),
                               actual_volatility_status='complete')
        else:
            missing.append(event)
    if missing:
        try:
            candles = load_candles()
            consistent = []
            for event in missing:
                raw = event.get('target_raw_json')
                if raw:
                    target = json.loads(raw)
                    last = candles.get(event['target']+3300)
                    if last is None or int(target[0])!=event['target'] or float(last[4])!=float(target[4]):
                        continue
                consistent.append(dict(event,target_candle_time=event['target']))
            assigned, _ = assign_rv(consistent,candles,config)
            for event in assigned:
                outcomes[event['target']].update(actual_volatility=event['rv'],actual_regime=event['regime'],
                                                actual_volatility_status='complete')
        except (ValueError, TypeError, KeyError, IndexError, OSError):
            # Missing/conflicting/invalid archives must not break a dashboard.
            pass
    return outcomes


def report(path):
    result = dict(status='Not started', config=None, config_hash=None, code_sha=None,
                  accepted=0, rejected=0, pending=0, all_hours=0, recorded_hours=0,
                  missing_hours=0, single_active=0, scored_events=0, methods=[], paired=[],
                  recent=[], recent_accepted=[], recent_predictions=[], missing=[], ensemble_membership=[])
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
            cfg = json.loads(state['config_json'])
            visible = {e['target']:e for e in events[-24:]}
            visible.update({e['target']:e for e in [r for r in events if r['gate']=='accepted'][-12:]})
            actuals = actual_volatility(list(visible.values()),c,tables,cfg['gate']['volatility_config'],time.time())
    except sqlite3.OperationalError:
        result['status'] = 'Phase 6 database unavailable'
        return result
    cfg = json.loads(state['config_json'])
    accepted = [e for e in events if e['gate']=='accepted']
    # Scheduled hours through the last recorded event include absent scheduler cycles.
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
        recent_accepted=[{**{k:e[k] for k in ('target','single_class','active_votes','actual_return','actual_direction')}, **actuals[e['target']], **gate_summary(e)} for e in accepted[-12:][::-1]],
        recent_predictions=[{k:p[k] for k in ('target','method','status','direction','p_up','p_down','version','model_version','correct','brier','signed_pnl','reason')} for p in predictions if p['target'] in {e['target'] for e in accepted[-12:]}],
        recent=[{**{k:e[k] for k in ('target','gate','single_class','active_votes','reason','actual_return')},
            **actuals[e['target']], 'actual_direction':e['actual_direction'], **gate_summary(e)} for e in events[-24:][::-1]],
        ensemble_membership=[dict(target=p['target'],method=p['method'],status=p['status'],
            eligible=(json.loads(p['audit_json']) or {}).get('eligible', []))
            for p in predictions if p['method'] in DIRECTION_ENSEMBLES],
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


# Presentation aliases only: persisted identifiers and frozen config stay intact.
METHOD_LABELS = {
    'always_up': 'Always UP', 'always_down': 'Always DOWN', 'random_50_50': 'Random 50/50',
    'momentum_1h': '1h Momentum Baseline', 'reversal_1h': '1h Reversal Baseline',
    'gpt': 'GPT / OpenAI', 'momentum_short': 'Short-horizon Momentum',
    'breakout': 'Breakout', 'trend': 'Trend', 'order_flow': 'Order Flow',
    'order_book': 'Order Book', 'logistic': 'Logistic Regression',
    'ensemble_vote': 'Majority-vote Direction Ensemble',
    'ensemble_probability': 'Probability-average Direction Ensemble',
}
CATEGORIES = (('Baselines', BASELINES), ('Direction Methods', DIRECTION_METHODS),
              ('Direction Ensembles', DIRECTION_ENSEMBLES))


def display_text(value):
    if isinstance(value, list):
        return ', '.join(str(display_text(member)) for member in value) or '—'
    if not isinstance(value, str):
        return value
    return METHOD_LABELS.get(value, value).replace(
        'missing ensembles excluded', 'incomplete 10-shot repeat agreement results excluded').replace(
        'Modal ACTIVE and >=8/10 required',
        'Repeat modal class = ACTIVE and ACTIVE repeat agreement >= 8/10 required')


def format_value(key, value, row=None):
    """Formatting only; report JSON and persisted numeric precision stay intact."""
    row = row or {}
    if key in ('actual_volatility','actual_regime') and value is None:
        return row.get('actual_volatility_status','pending')
    if key in ('actual_return','actual_direction') and value is None:
        return 'pending'
    if value is None:
        return '—'
    if key in ('actual_return','signed_pnl','mean_signed_pnl','pnl_difference'):
        return f'{value:+.4f}%'
    if key in ('actual_volatility','mean_absolute_return','median_absolute_return'):
        return f'{value:.4f}%'
    if key in ('accuracy','coverage_all','coverage_active','coverage_accepted','accuracy_difference'):
        return f'{value:.1%}'
    if key in ('p_up','p_down'):
        return f'{value:.3f}'
    if key in ('brier','brier_difference'):
        return f'{value:.4f}'
    if key == 'exact_mcnemar_p':
        return f'{value:.4g}'
    if isinstance(value,float):
        return f'{value:.4f}'
    return display_text(value)


def display_records(rows):
    labels = {'single_class': 'Single-shot volatility', 'active_votes': 'ACTIVE repeat agreement',
              'repeat_modal_class': 'Repeat modal class', 'repeat_agreement': 'Repeat agreement',
              'eligible': 'Eligible Direction Methods'}
    if not rows:
        return records_table([])
    keys = list(dict.fromkeys(k for row in rows for k in row if k != 'actual_volatility_status'))
    return styled_table(
        [labels.get(k,k.replace('_',' ').capitalize()) for k in keys],
        [[('—' if row.get(k) is None else str(row[k])+'/10') if k == 'active_votes'
          else ('—' if row.get(k) is None else f'{round(row[k]*10)}/10') if k == 'repeat_agreement'
          else format_value(k,row.get(k),row) for k in keys] for row in rows])


def page(path, analysis=False):
    from html import escape
    data = report(path)
    introduction = ('<h1>Active Direction — Phase 6</h1>'
        '<p>Prospective high-confidence-ACTIVE direction prediction. '
        'Phase 1–3 retrospective ACTIVE accuracy was approximately 34.9% for OpenAI and 41.9% for Jev. '
        'ACTIVE is not assumed easier: this experiment compares alternative conditional signals prospectively.</p>')
    gate_help = ('<p>Single-shot volatility and 10-shot repeat agreement use the same saved snapshot. '
        'All three conditions are required:</p>'
        '<ul><li>Single-shot volatility = ACTIVE</li>'
        '<li>Repeat modal class = ACTIVE</li>'
        '<li>Repeat agreement &gt;= 8/10</li></ul>'
        '<p>All ten repeats must be valid; ACTIVE repeat agreement &gt;= 8/10 is required. '
        'Effective sample count is accepted distinct target hours, not API calls. '
        'Stop after 96 accepted distinct target hours, including gates with missing direction outputs; no optional stopping.</p>')
    counts = metric_blocks([('Accepted / 96',str(data['accepted'])+' / 96'),
        ('Rejected / NO SIGNAL',data['rejected']),('Pending',data['pending']),('Scored ACTIVE events',data['scored_events']),
        ('All scheduled hours',data['all_hours']),('Missing hours',data['missing_hours']),('Single-shot ACTIVE hours',data['single_active'])])
    gate_content = '<p>'+escape(data['status'])+'</p>'+gate_help+counts
    if data['recent']:
        current = data['recent'][0]
        gate_content += '<h3>Latest saved gate</h3>'+display_records([{k:current[k] for k in (
            'target','gate','single_class','repeat_modal_class','repeat_agreement','active_votes','reason')}])
    content = introduction+card('Volatility Gate',gate_content)
    fields = ('method','n','correct','accuracy','brier_n','brier','pnl_n','mean_signed_pnl',
              'mean_absolute_return','median_absolute_return','coverage_all','coverage_active','coverage_accepted')
    headers = ('Direction predictor', 'N', 'Correct', 'Accuracy', 'Brier N', 'Brier', 'PnL N',
         'Mean signed PnL', 'Mean absolute return', 'Median absolute return',
         'Coverage / all hours', 'Coverage / single-shot ACTIVE hours', 'Coverage / accepted hours')
    category_help = {
        'Baselines': '<p>Comparison-only baselines; never included in a Direction ensemble. '
            'Always UP / Always DOWN are constant directions. Random 50/50 saves one fixed-seed '
            'reproducible direction per target. 1h Momentum Baseline follows the previous completed '
            '1h return sign; 1h Reversal Baseline takes its opposite. Exactly zero return: abstain.</p>',
        'Direction Methods': '<p>GPT / OpenAI, Short-horizon Momentum, Breakout, Trend, Order Flow, '
            'Order Book and Logistic Regression are the only permitted Direction ensemble members. '
            'Short-horizon Momentum uses the mean of 5m and 15m returns and is separate from 1h Momentum Baseline.</p>',
        'Direction Ensembles': '<p>Majority-vote Direction Ensemble needs at least three eligible Direction Methods: '
            'more UP votes predicts UP, more DOWN votes predicts DOWN; tie: abstain. '
            'Probability-average Direction Ensemble needs at least two eligible probability Direction Methods: '
            'p_up &gt; 0.5 predicts UP, p_up &lt; 0.5 predicts DOWN; p_up = 0.5: abstain. '
            'Rules have no invented probabilities. Each participating Direction Method has equal weight. '
            'Baselines never participate.</p>'}
    for title, members in CATEGORIES:
        rows = [[format_value(k,m[k]) for k in fields] for m in data['methods'] if m['method'] in members]
        # Keep all pre-start method labels visible before a start marker exists.
        if not rows:
            rows = [[METHOD_LABELS[m]]+['—']*(len(fields)-1) for m in members]
        body = category_help[title]+styled_table(headers,rows)
        if title == 'Direction Ensembles':
            body += '<h3>Saved Direction ensemble membership</h3>'+display_records(data['ensemble_membership'][-24:])
        content += card(title,body)
    content += card('Direction target and evaluation',
        '<p>Same next-hour direction target as Phases 1–3. UP: target completed 1h close &gt; reference completed 1h close. '
        'DOWN: target completed 1h close &lt; reference completed 1h close. '
        'FLAT: target completed 1h close = reference completed 1h close. No epsilon dead-zone.</p>'
        '<p>Accuracy and coverage are shown as percentages. FLAT is excluded from Accuracy / Brier and contributes zero simple PnL. '
        'Predictor abstain is excluded from scored N, never counted as incorrect, and reduces coverage. '
        'Brier is scored only for available probability forecasts. '
        'PnL = ± target percent return for unit long/short, no fees/spread. '
        'Coverage uses available predictions, all scheduled hours (missing cycles included), observed single-shot ACTIVE '
        'hours and accepted gates. Missing-hour ACTIVE status is unknown.</p>')
    if analysis:
        content += card('Random reference values · analysis only',
            '<p>Theoretical Random 50/50 expected accuracy = 50%. '
            'Constant p_up = 0.5 forecast binary Brier = 0.25 for every UP/DOWN target. '
            'These are reference values, separate from the saved Random 50/50 direction baseline. '
            'No live Monte Carlo predictor is run.</p>')
        content += card('Paired comparisons on common events',
            '<p>Two-sided exact McNemar p-values appear from 20 common scored events. '
            'Exploratory comparisons are unadjusted for multiple testing; differences are left minus right. '
            'Brier differences use the probability intersection only.</p>'+display_records(data['paired']))
    content += card('Recent accepted ACTIVE events',display_records(data['recent_accepted']))
    content += card('Recent accepted-event predictor outputs',display_records(data['recent_predictions']))
    content += card('Recent hours · accepted and rejected',
        '<p>Actual volatility uses the Phase 4 RV definition and the frozen Phase 4 thresholds for Actual regime. '
        'Saved Phase 4/5 target evaluations or complete saved 5m windows only; no fetching or interpolation. '
        'Open hours are pending; missing/conflicting completed-hour data is unavailable.</p>'+display_records(data['recent']))
    content += card('Missing / abstaining outputs · never backfilled',display_records(data['missing'][-60:]))
    if data['config']:
        content += card('Frozen preregistration', '<p>Started: '+escape(data['started_at'])+' · Code: '+escape(data['code_sha'])+
            ' · Config hash: '+escape(data['config_hash'])+'</p><p>Terminology normalized for display; stored preregistration is unchanged.</p>'
            '<details><summary>Frozen design</summary><pre>'+
            escape(display_text(json.dumps(data['config'],indent=2,sort_keys=True)))+'</pre></details>')
    return page_shell('Active Direction — Phase 6',content,'analysis' if analysis else 'phase6',
                      phase='phase6' if analysis else None,refresh=300)
