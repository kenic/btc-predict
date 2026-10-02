"""Read-only Phase 4 dashboard and analysis; no calls to price or prediction APIs."""
import html
import json
import sqlite3
from phase4 import DB_PATH, segment_rows
from volatility import CLASSES, winner, brier

SEGMENTS = (('phase4a', 'Phase 4a — predictions 1–48 per predictor'),
            ('phase4b', 'Phase 4b — predictions 49–96 per predictor'),
            ('combined', 'Combined — all Phase 4 predictions'))

def esc(value):
    return html.escape(str(value))

def page(analysis=False):
    c=sqlite3.connect(DB_PATH.resolve().as_uri()+'?mode=ro',uri=True)
    c.row_factory=sqlite3.Row
    try:
        has_schema=c.execute("SELECT 1 FROM sqlite_master WHERE name='volatility_predictions'").fetchone()
        has_segments=c.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='phase4_segments'").fetchone()
        source='phase4_segments' if has_segments else 'volatility_predictions'
        rows=[dict(r) for r in c.execute(f'SELECT * FROM {source} ORDER BY target_candle_time DESC,predictor')] if has_schema else []
        if not has_segments:
            rows=segment_rows(rows)
        configs=[dict(r) for r in c.execute('SELECT * FROM phase4_configs')] if has_schema else []
        phase3=[tuple(r) for r in c.execute("SELECT predictor,count(*),sum(evaluated_at IS NOT NULL) FROM predictions WHERE phase='phase3' GROUP BY predictor")]
    finally:
        c.close()
    config=json.loads(configs[0]['config_json']) if configs else None
    def table(headers, data):
        return '<div class="scroll"><table><tr>'+''.join('<th>'+esc(h)+'</th>' for h in headers)+'</tr>'+''.join('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in row)+'</tr>' for row in data)+'</table></div>'
    def stats(items, get_p):
        n=len(items)
        return [n, f"{sum(winner(get_p(r))==r['actual_class'] for r in items)/n:.1%}" if n else '—',
                f"{sum(brier(get_p(r),r['actual_class']) for r in items)/n:.4f}" if n else '—']
    content='<p>Phase 3: '+esc(phase3)+'</p>'
    if config:
        content+=f"<p>Frozen thresholds: QUIET &lt; {config['quiet_upper']:.6f}%; NORMAL &lt; {config['active_lower']:.6f}%; otherwise ACTIVE.</p>"
        content+='<p>RV = 100 × √Σ log(Cᵢ/Cᵢ₋₁)², twelve consecutive 5m returns; not annualized. '+esc(config['historical_hours'])+' calibration hours. '+('Enabled' if (DB_PATH.parent/'phase4.enabled').exists() else 'Prepared, disabled')+'</p>'
        content+='<details><summary>Frozen calibration provenance</summary><pre>'+esc(json.dumps(config,indent=2))+'</pre></details>'
    else:
        content+='<p>Phase 4 awaits Phase 3 closure and frozen historical thresholds. Prediction is disabled.</p>'
    def model_p(r):
        return [r['p_quiet'],r['p_normal'],r['p_active']]
    content+='<h2>Cohort comparison</h2><p>Saved predictions are numbered independently for each predictor, including pending evaluations. Missed hours are not backfilled. Accuracy and Brier use evaluated rows only. Phase 4b continues the unchanged frozen design; combined results are a descriptive aggregate.</p>'
    content+='<nav aria-label="Phase 4 cohorts">'+''.join(f'<a href="#{segment}">{esc(title)}</a>' for segment,title in SEGMENTS)+'</nav>'
    comparison=[]
    for segment,title in SEGMENTS:
        for model in ('openai','jev'):
            saved=[r for r in rows if r['predictor']==model and (segment=='combined' or r['cohort']==segment)]
            evaluated=[r for r in saved if r['actual_class'] in CLASSES]
            comparison.append([title,model,len(saved),len(saved)-len(evaluated),*stats(evaluated,model_p)])
    content+=table(['Cohort','Predictor','Predicted','Pending','Evaluated','Accuracy','Multiclass Brier'],comparison)
    for segment, title in SEGMENTS:
        content+=f'<section id="{segment}" aria-labelledby="{segment}-title"><h2 id="{segment}-title">'+title+'</h2>'
        segment_items=[r for r in rows if segment=='combined' or r['cohort']==segment]
        summaries=[]
        for model in ('openai','jev'):
            all_model=[r for r in segment_items if r['predictor']==model]
            evaluated=[r for r in all_model if r['actual_class'] in CLASSES]
            summaries.append([model+' model',len(all_model),*stats(evaluated,model_p)])
            if config:
                summaries.append([model+' majority baseline',len(all_model),*stats(evaluated,lambda r:[int(x==config['majority_class']) for x in CLASSES])])
                summaries.append([model+' persistence baseline',len(all_model),*stats(evaluated,lambda r:[int(x==r['persistence_class']) for x in CLASSES])])
        content+='<h2>Scores and baselines</h2><p>Baseline comparisons use the same evaluated hours as each model. Majority class is fixed from calibration data. Brier is the sum over three classes (range 0–2; uniform baseline 2/3).</p>'+table(['Predictor','Predicted','Evaluated','Accuracy','Multiclass Brier'],summaries)
        if analysis:
            for model in ('openai','jev'):
                selected=[r for r in segment_items if r['predictor']==model and r['actual_class'] in CLASSES]
                content+='<h2>'+esc(model)+' confusion matrix</h2><p>Rows: actual; columns: predicted. Ties resolve QUIET, NORMAL, ACTIVE in that order.</p>'
                content+=table(['Actual / predicted',*CLASSES],[[a,*[sum(r['actual_class']==a and winner(model_p(r))==p for r in selected) for p in CLASSES]] for a in CLASSES])
                calibration=[]
                for i,label in enumerate(CLASSES):
                    for bucket in range(10):
                        group=[r for r in selected if bucket/10<=model_p(r)[i] and (model_p(r)[i]<(bucket+1)/10 or bucket==9)]
                        n=len(group)
                        calibration.append([label,f'{bucket*10}–{(bucket+1)*10}%',n,
                            f'{sum(model_p(r)[i] for r in group)/n:.1%}' if n else '—',
                            f'{sum(r["actual_class"]==label for r in group)/n:.1%}' if n else '—'])
                content+='<h2>'+esc(model)+' class calibration</h2>'+table(['Class','Probability bucket','N','Mean forecast','Observed frequency'],calibration)
        content+='</section>'
    from datetime import datetime, timezone
    content+='<h2>Prediction history</h2>'+table(['Cohort','Prediction #','Target UTC','Model','QUIET','NORMAL','ACTIVE','Forecast','Actual RV %','Actual','Reason'],[
        [r['cohort'],r['prediction_number'],datetime.fromtimestamp(r['target_candle_time'],timezone.utc).strftime('%Y-%m-%d %H:%M'),r['predictor'],
         *[f'{p:.1%}' for p in model_p(r)],winner(model_p(r)),f"{r['actual_rv']:.6f}" if r['actual_rv'] is not None else '—',r['actual_class'] or 'Pending',r['reason'] or ''] for r in rows])
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BTC Phase 4 — Volatility</title><style>body{font:16px system-ui;background:#f5f5f7;color:#222;margin:24px auto;padding:0 20px;max-width:1100px}h1,h2{margin-top:32px}p{line-height:1.6}a{color:#245bb2}table{border-collapse:collapse;background:white;width:100%}td,th{padding:12px;border-bottom:1px solid #ddd;text-align:left}pre{white-space:pre-wrap}.scroll{overflow:auto}nav{display:flex;gap:20px;flex-wrap:wrap}</style><nav><a href="/">Phase 4 dashboard</a><a href="/analyze/?phase=phase4">Phase 4 analysis</a><a href="/direction/">Phase 1–3 dashboard</a><a href="/analyze/">Phase 1–3 analysis</a></nav><h1>BTC Phase 4 — Realized volatility</h1>'+content+'</html>'
