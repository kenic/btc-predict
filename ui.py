"""Shared, presentation-only HTML helpers. Content passed to shell/card is trusted HTML."""
from html import escape

NAV = (('current', '/', 'Current'), ('direction', '/direction/', 'Direction'),
       ('volatility', '/volatility/', 'Volatility'), ('repeated', '/repeated/', 'Repeated'),
       ('regression', '/regression/', 'Regression'), ('phase6', '/direction-active/', 'Active Direction'), ('analysis', '/analyze/', 'Analysis'))


def analysis_filters(phase):
    choices = (('all', 'All direction phases'), ('phase1', 'Phase 1'), ('phase2', 'Phase 2'),
               ('phase3', 'Phase 3'), ('phase4', 'Volatility'), ('phase4r', 'Repeated'), ('phase5', 'Regression'), ('phase6', 'Active Direction'))
    return '<nav class="filters" aria-label="Analysis experiment">' + ''.join(
        f'<a href="/analyze/?phase={key}"' + (' aria-current="page"' if key == phase else '') + f'>{label}</a>'
        for key, label in choices) + '</nav>'


def page_shell(title, content, active, *, phase=None, refresh=None):
    nav = '<nav class="site-nav" aria-label="Main navigation">' + ''.join(
        f'<a href="{url}"' + (' aria-current="page"' if key == active else '') + f'>{label}</a>'
        for key, url, label in NAV) + '</nav>'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            + (f'<meta http-equiv="refresh" content="{refresh}">' if refresh else '')
            + f'<title>BTC Predictor — {escape(title)}</title>'
            '<link rel="icon" type="image/png" href="/static/btc.png">'
            '<link rel="apple-touch-icon" href="/static/btc.png">'
            '<link rel="stylesheet" href="/static/style.css"></head><body><div class="container">'
            '<header><a class="brand" href="/"><img src="/static/btc.png" alt="Bitcoin">BTC Predictor</a>'
            + nav + '</header><main>' + (analysis_filters(phase) if active == 'analysis' else '')
            + content + '</main><footer class="footer">BTC-USD · Experimental probability forecasting.<br>'
            'Not financial advice.</footer></div></body></html>')


def card(title, content):
    return f'<section><h2>{escape(title)}</h2>{content}</section>'


def metric_blocks(items):
    def display(value):
        return '—' if value is None else f'{value:.4f}' if isinstance(value, float) else str(value)
    return '<div class="metric-grid">' + ''.join(
        f'<div><div class="stat-value">{escape(display(value))}</div><div class="stat-label">{escape(label)}</div></div>'
        for label, value in items) + '</div>'


def styled_table(headers, rows):
    return '<div class="table-scroll"><table><thead><tr>' + ''.join(
        f'<th>{escape(str(h))}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join(f'<td>{escape(str(v))}</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def records_table(items):
    if not items:
        return '<p class="muted">No rows available yet.</p>'
    keys = list(dict.fromkeys(key for row in items for key in row))
    return styled_table([key.replace('_', ' ').capitalize() for key in keys],
                        [[row.get(key, '—') for key in keys] for row in items])
