"""Read-only dashboards for Claude Phase 5c / 6c side observations."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import sqlite3
import time

from ui import card, metric_blocks, page_shell, styled_table


def report(source_path, section='phase5c', *, clock=None):
    if section not in ('phase5c', 'phase6c'):
        raise ValueError('Unknown Claude observation section')
    side_path = Path(source_path).with_name('claude-observation.db')
    try:
        with closing(sqlite3.connect(side_path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)) as c:
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'claude_config', 'phase5c_predictions', 'phase6c_events', 'phase6c_calls'}
            if not required <= tables:
                return dict(status='awaiting_init', message='Claude observation has not been initialized.')
            config = c.execute('SELECT * FROM claude_config WHERE singleton=1').fetchone()
            if config is None:
                return dict(status='awaiting_init', message='Claude observation has not been initialized.')
            cfg = json.loads(config['config_json'])
            params = cfg['params']
            model, max_tokens = params['model'], params['max_tokens']
            # Public fields only: never expose input context, prompt, raw response or DB paths.
            rv = [dict(r) for r in c.execute('''SELECT target,status,previous_rv,predicted_rv,actual_rv,
                evaluated_at,model_version,error FROM phase5c_predictions ORDER BY target''')]
            events = [dict(r) for r in c.execute('''SELECT cohort,target,actual_direction,actual_return,
                evaluated_at FROM phase6c_events ORDER BY target,cohort''')]
            calls = [dict(r) for r in c.execute('''SELECT cohort,target,slot,status,direction,p_up,p_down,
                correct,brier,signed_pnl,model_version,error FROM phase6c_calls ORDER BY target,cohort,slot''')]
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return dict(status='unavailable', message='Claude observation data is currently unavailable.')
    primary = [r for r in calls if r['slot'] == 0]
    repeats = [r for r in calls if r['slot'] > 0]
    repeat_groups = defaultdict(list)
    for row in repeats:
        repeat_groups[row['cohort'], row['target']].append(row)
    summaries = {}
    for event in events:
        key = event['cohort'], event['target']
        rows = repeat_groups[key]
        votes = Counter(r['direction'] for r in rows if r['status'] == 'complete')
        n = sum(votes.values())
        full = n == 10
        summaries[key] = dict(successful_repeats=n, repeat_slots=len(rows),
            modal_direction=('UP' if votes['UP'] > votes['DOWN'] else 'DOWN') if full and votes['UP'] != votes['DOWN'] else None,
            agreement=max(votes.values(), default=0)/10 if full else None,
            tie=full and votes['UP'] == votes['DOWN'])
    event_lookup = {(r['cohort'], r['target']): r for r in events}
    recent_direction = []
    for row in reversed(primary[-100:]):
        key = row['cohort'], row['target']
        recent_direction.append(dict(row, actual_direction=event_lookup[key]['actual_direction'],
            actual_return=event_lookup[key]['actual_return'], **summaries[key]))
    comparison = None
    comparison_status = 'unavailable'
    try:
        # Frozen analysis is read-only; do not change it while observation is running.
        from claude_analysis import report as comparison_report
        result = comparison_report(side_path, source_path)
        comparison = ({'rv': result['rv']} if section == 'phase5c' else {
            k: result[k] for k in ('direction', 'by_cohort', 'repeat_agreement_performance',
                                   'same_different', 'same_different_cases')})
        if section == 'phase6c':
            comparison['same_different_cases'] = comparison['same_different_cases'][-100:]
        comparison_status = 'ready'
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        pass
    clock = time.time() if clock is None else clock
    pending = sum(r['evaluated_at'] is None for r in rv)
    direction_pending = sum(r['evaluated_at'] is None for r in events)
    lifecycle = ('scheduled' if clock < config['start_target'] else 'observing' if clock < config['end_exclusive']
                 else 'recovering_actuals' if pending or direction_pending else 'window_closed')
    return dict(status='ready', section=section, lifecycle=lifecycle,
        start_target=config['start_target'], end_exclusive=config['end_exclusive'],
        model=model, temperature=params.get('temperature'),
        thinking=params.get('thinking', {}).get('type'), effort=params.get('output_config', {}).get('effort'),
        max_tokens=max_tokens,
        planned=(config['end_exclusive']-config['start_target'])//3600,
        recorded=len(rv), rv_counts=dict(Counter(r['status'] for r in rv)),
        actuals_available=len(rv)-pending, actuals_pending=pending,
        accepted_targets=len(events), primary_counts=dict(Counter(r['status'] for r in primary)),
        repeat_counts=dict(Counter(r['status'] for r in repeats)),
        complete_repeat_sets=sum(r['successful_repeats'] == 10 for r in summaries.values()),
        direction_actuals_available=len(events)-direction_pending, direction_actuals_pending=direction_pending,
        comparison_status=comparison_status, comparison=comparison,
        recent=([dict(r) for r in reversed(rv[-100:])] if section == 'phase5c' else recent_direction))


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def number(value, scale=1):
    return '—' if value is None else f'{value*scale:.4f}'


def table_or_empty(headers, rows, message='No scored observations yet.'):
    return styled_table(headers, rows) if rows else '<p class="muted">'+escape(message)+'</p>'


def direction_table(groups):
    rows = []
    for label, methods in groups:
        for method, scores in methods.items():
            rows.append([label, {'gpt':'GPT', 'logistic':'Logistic', 'claude':'Claude',
                                'primary':'Claude primary', 'modal':'Claude repeat modal'}.get(method, method),
                         scores['n'], scores['accuracy_n'], number(scores['accuracy'], 100),
                         scores['brier_n'], number(scores['brier']), number(scores['mean_signed_pnl']),
                         number(scores['total_signed_pnl'])])
    return table_or_empty(('Population', 'Method', 'N', 'Accuracy N', 'Accuracy (%)', 'Brier N',
                           'Brier', 'Mean signed PnL (%)', 'Total signed PnL (%)'), rows)


def page(source_path, section='phase5c', analysis=False):
    data = report(source_path, section)
    title = 'Phase 5c — Claude hourly RV' if section == 'phase5c' else 'Phase 6c — Claude ACTIVE direction'
    content = '<h1>'+title+'</h1>'
    content += '<p class="muted">Claude side observation. Fixed calendar window; no optional stopping. All comparisons are exploratory.</p>'
    content += '<p><a href="/phase5b/">Phase 5b</a> · <a href="/phase5c/">Phase 5c</a> · <a href="/direction-active/">Phase 6</a> · <a href="/phase6b/">Phase 6b</a> · <a href="/phase6c/">Phase 6c</a></p>'
    if data['status'] != 'ready':
        content += '<p>'+escape(data['message'])+'</p>'
        return page_shell(title, content, 'analysis' if analysis else section,
                          phase=section if analysis else None, refresh=60)
    lifecycle = {'scheduled':'Awaiting first target', 'observing':'Observing',
                 'recovering_actuals':'Prediction window closed; recovering actuals', 'window_closed':'Window closed'}
    content += card('Observation window', metric_blocks([
        ('Status', lifecycle[data['lifecycle']]), ('First target', utc(data['start_target'])),
        ('New prediction end (exclusive)', utc(data['end_exclusive'])), ('Model', data['model']),
        ('Temperature', data['temperature'] if data['temperature'] is not None else 'Provider default'),
        ('Thinking / effort', f"{data['thinking'] or 'Default'} / {data['effort'] or 'Default'}"),
        ('Max output tokens', data['max_tokens'])]))
    if section == 'phase5c':
        counts = data['rv_counts']
        content += card('Hourly observation progress', metric_blocks([
            ('Recorded / planned slots', f"{data['recorded']} / {data['planned']}"),
            ('On-time predictions', counts.get('complete',0)), ('Waiting for 5b input', counts.get('waiting',0)),
            ('Started / uncertain', counts.get('started',0)), ('Failed', counts.get('failed',0)),
            ('Missed', counts.get('missed',0)), ('Late', counts.get('late',0)),
            ('Actual RV available', data['actuals_available']), ('Actual RV pending', data['actuals_pending'])]))
        content += '<p class="muted">All hourly targets, including QUIET / NORMAL / ACTIVE. RV is percent, nonannualized, from 12 consecutive 5m log returns. Missing slots remain missing.</p>'
        if data['comparison'] is not None:
            scores = data['comparison']['rv']
            rows = [[name, scores[key]['n'], number(scores[key]['mae']), number(scores[key]['bias']),
                     number(scores[key]['correlation'])] for key,name in (('gpt','GPT 5b'),('claude','Claude 5c'))]
            content += card('GPT RV vs Claude RV — same targets and snapshots', table_or_empty(
                ('Model','Paired N','MAE (RV percentage points)','Bias (RV percentage points)','Correlation with actual RV'), rows)
                + metric_blocks([('GPT / Claude prediction correlation', number(scores['gpt_claude_correlation']))]))
            content += '<p class="muted">Pairs require complete on-time predictions, identical saved input and the same actual RV. Bias = predicted minus actual. Correlation needs at least two nonconstant observations.</p>'
        rows = [[utc(r['target']), r['status'], number(r['previous_rv']), number(r['predicted_rv']),
                 number(r['actual_rv']), 'Available' if r['evaluated_at'] else 'Pending',
                 r['model_version'] or '—', r['error'] or '—'] for r in data['recent']]
        content += card('Latest 100 hourly targets', table_or_empty(
            ('Target start (UTC)','Status','Previous RV (%)','Claude RV (%)','Actual RV (%)','Actual evaluation','Model','Error'),
            rows, 'Awaiting the first scheduled target.'))
    else:
        counts = data['primary_counts']
        content += card('Accepted ACTIVE observations', metric_blocks([
            ('Accepted targets observed', data['accepted_targets']), ('Primary on-time', counts.get('complete',0)),
            ('Primary started / uncertain', counts.get('started',0)), ('Primary failed', counts.get('failed',0)),
            ('Primary missed', counts.get('missed',0)), ('Primary late', counts.get('late',0)),
            ('Complete ten-repeat sets', data['complete_repeat_sets']),
            ('Repeat calls on time', data['repeat_counts'].get('complete',0)),
            ('Repeat calls failed', data['repeat_counts'].get('failed',0)),
            ('Repeat calls missed', data['repeat_counts'].get('missed',0)),
            ('Actuals available', data['direction_actuals_available']), ('Actuals pending', data['direction_actuals_pending'])]))
        content += '<p class="muted">Only the same accepted targets and snapshots from Phase 6 / 6b. One primary forecast plus ten additional repeats. Claude and repeat agreement never enter the ACTIVE gate or ensembles.</p>'
        if data['comparison'] is not None:
            result = data['comparison']
            content += card('GPT / Logistic / Claude — common scored targets',
                metric_blocks([('Common-target N', result['direction']['common_target_n'])])
                + direction_table([('All cohorts', result['direction']['methods'])]
                    + [(cohort, group['methods']) for cohort, group in result['by_cohort'].items()]))
            content += '<p class="muted">All three methods must be complete and scored on the same target and snapshot. FLAT outcomes have zero PnL and no accuracy/Brier score. PnL is percent without fees.</p>'
            content += card('Performance by Claude repeat agreement', direction_table([
                (f"{float(agreement)*100:.0f}% agreement", methods)
                for agreement, methods in result['repeat_agreement_performance'].items()]))
            content += '<p class="muted">Agreement requires all ten on-time repeats. A 5–5 tie has no modal forecast; incomplete sets have no agreement. Modal performance uses evaluated targets with a successful primary; modal votes have no Brier probability.</p>'
            content += card('GPT / Claude same vs different directions', direction_table([
                (label, {m:group[m] for m in ('gpt','claude')}) for label,group in result['same_different'].items()]))
            cases = result['same_different_cases']
            content += card('Latest 100 GPT / Claude paired cases', table_or_empty(
                ('Target (UTC)','Cohort','Case','Actual','GPT','GPT p(UP)','Claude','Claude p(UP)','GPT PnL (%)','Claude PnL (%)'),
                [[utc(r['target']),r['cohort'],r['case'],r['actual'],r['gpt_direction'],number(r['gpt_p_up']),
                  r['claude_direction'],number(r['claude_p_up']),number(r['gpt_signed_pnl']),number(r['claude_signed_pnl'])]
                 for r in reversed(cases)]))
        rows = [[utc(r['target']),r['cohort'],r['status'],r['direction'] or '—',number(r['p_up']),number(r['p_down']),
                 r['actual_direction'] or 'Pending',number(r['correct'],100),number(r['brier']),number(r['signed_pnl']),
                 f"{r['successful_repeats']} / 10",'Tie' if r['tie'] else r['modal_direction'] or '—',
                 number(r['agreement'],100),r['error'] or '—'] for r in data['recent']]
        content += card('Latest 100 accepted targets', table_or_empty(
            ('Target (UTC)','Cohort','Primary status','Claude','p(UP)','p(DOWN)','Actual','Correct (%)','Brier',
             'Signed PnL (%)','Successful repeats','Modal','Agreement (%)','Error'), rows,
            'Awaiting an accepted ACTIVE target after registration.'))
    if data['comparison_status'] != 'ready':
        content += '<p class="muted">Comparison data is currently unavailable. Observation progress is shown independently.</p>'
    return page_shell(title, content, 'analysis' if analysis else section,
                      phase=section if analysis else None, refresh=60)
