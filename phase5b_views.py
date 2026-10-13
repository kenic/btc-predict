"""Read-only Phase 5b dashboard; rendering never initializes or runs experiments."""
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import sqlite3

from phase5b_analysis import JOIN_SQL, summarize
from ui import card, metric_blocks, page_shell, styled_table


def report(path):
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)) as c:
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'phase5b_config', 'phase5b_predictions'} <= tables:
                return dict(status='awaiting_init', message='Phase 5b has not been initialized.')
            config = c.execute('SELECT * FROM phase5b_config WHERE singleton=1').fetchone()
            if config is None:
                return dict(status='awaiting_init', message='Phase 5b has not been initialized.')
            predictions = [dict(r) for r in c.execute('SELECT * FROM phase5b_predictions ORDER BY target_candle_time')]
            has_phase6 = {'phase6_events', 'phase6_predictions'} <= tables
            if has_phase6:
                joined = [dict(r) for r in c.execute(JOIN_SQL)]
            else:
                joined = [dict(target_candle_time=r['target_candle_time'], predicted_rv=r['predicted_rv'],
                               status=r['status'], gpt_status=None, logistic_status=None,
                               gpt_pnl=None, logistic_pnl=None, gpt_correct=None, logistic_correct=None)
                          for r in predictions]
    except (sqlite3.Error, OSError):
        return dict(status='unavailable', message='Phase 5b data is currently unavailable.')
    counts = {}
    for row in predictions:
        counts[row['status']] = counts.get(row['status'], 0)+1
    # Expose dashboard fields only, not complete prompts/raw provider payloads.
    fields = ('target_candle_time', 'status', 'previous_rv', 'predicted_rv', 'actual_rv',
              'evaluated_at', 'model_version', 'error')
    return dict(status='ready', version=config['version'], start_target=config['start_target'],
                end_target=config['end_target'], planned=500, recorded=len(predictions),
                remaining=max(0, 500-len(predictions)), counts=counts,
                actuals_available=sum(r['evaluated_at'] is not None for r in predictions),
                actuals_pending=sum(r['evaluated_at'] is None for r in predictions),
                phase6_available=has_phase6, comparison=summarize(joined),
                recent=[{k:r[k] for k in fields} for r in reversed(predictions[-100:])])


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def number(value, scale=1):
    return '—' if value is None else f'{value*scale:.4f}'


def page(path, analysis=False):
    data = report(path)
    title = 'Phase 5b — Hourly RV observation'
    content = '<h1>'+title+'</h1>'
    content += '<p class="muted">Independent observation of 500 consecutive hourly targets, including QUIET / NORMAL / ACTIVE. Failed and missed targets count toward the fixed window. No optional stopping or selector.</p>'
    if data['status'] != 'ready':
        content += '<p>'+escape(data['message'])+'</p>'
    else:
        counts = data['counts']
        content += card('Fixed observation window', metric_blocks([
            ('Version', data['version']), ('First target', utc(data['start_target'])),
            ('Last target', utc(data['end_target'])), ('Recorded / planned', f"{data['recorded']} / {data['planned']}"),
            ('Remaining slots to record', data['remaining']), ('On-time predictions', counts.get('complete',0)),
            ('Failed', counts.get('failed',0)), ('Missed', counts.get('missed',0)),
            ('Started / uncertain', counts.get('started',0)), ('Late responses', counts.get('late',0)),
            ('Actual RV available', data['actuals_available']), ('Actual RV pending', data['actuals_pending'])]))
        content += '<p class="muted">RV is percent, nonannualized: 12 consecutive 5m log returns from 13 closes. Missing candles remain pending; no interpolation or zero-fill. Recording all 500 slots does not imply all predictions or actuals are available.</p>'
        comparison = data['comparison']
        content += card('GPT vs Logistic by predicted RV percentile',
                        metric_blocks([('On-time percentile population', comparison['percentile_population'])]))
        content += '<p class="muted">Exploratory comparison. Percentile cuts use all valid on-time Phase 5b predictions. Phase 6 direction outcomes are gate-selected and may cover only part of the window. Equal cut values enter the higher bin. PnL is percent, unit long/short, without fees. Flat returns are excluded from accuracy N.</p>'
        if data['recorded'] < data['planned']:
            content += '<p class="muted">Collection is incomplete; percentile boundaries may change as more predictions arrive.</p>'
        if not data['phase6_available']:
            content += '<p class="muted">Phase 6 comparison data is not available yet.</p>'
        headers = ('Percentile', '5b observations', 'Method', 'N', 'Accuracy N', 'Accuracy (%)', 'Mean signed PnL (%)', 'Total PnL (%)')
        for population, label in (('paired', 'Common scored targets'), ('available', 'Each method’s available scored targets')):
            rows = []
            for group in comparison['bins']:
                for method in ('gpt', 'logistic'):
                    metrics = group[method][population]
                    rows.append([group['percentile'], group['observations'], 'GPT' if method=='gpt' else 'Logistic',
                                 metrics['N'], metrics['accuracy_N'], number(metrics['accuracy'],100),
                                 number(metrics['mean_signed_pnl']), number(metrics['total_pnl'])])
            content += card(label, styled_table(headers, rows) if rows else '<p class="muted">No on-time predictions yet.</p>')
        rows = [[utc(r['target_candle_time']), r['status'], number(r['previous_rv']),
                 number(r['predicted_rv']), number(r['actual_rv']),
                 'Available' if r['evaluated_at'] is not None else 'Pending',
                 r['model_version'] or '—', r['error'] or '—'] for r in data['recent']]
        content += card('Latest 100 hourly targets', styled_table(
            ('Target start (UTC)', 'Status', 'Previous RV (%)', 'Predicted RV (%)', 'Actual RV (%)',
             'Actual evaluation', 'Model', 'Error'), rows) if rows else '<p class="muted">Awaiting the first scheduled target.</p>')
    return page_shell(title, content, 'analysis' if analysis else 'phase5b',
                      phase='phase5b' if analysis else None, refresh=300)
