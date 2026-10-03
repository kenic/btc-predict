"""Read-only later-stage metrics; partial ensembles are never scored."""
import html
import json
import math
import sqlite3
import statistics
from phase4 import DB_PATH
from volatility import brier, winner


def metrics(rows, key):
    errors = [r[key]-r['actual_rv'] for r in rows]
    n = len(errors)
    return {'n': n, 'mae': sum(map(abs,errors))/n if n else None,
            'rmse': math.sqrt(sum(e*e for e in errors)/n) if n else None,
            'bias': sum(errors)/n if n else None}


def correlation(x,y):
    if len(x)<2 or len(set(x))<2 or len(set(y))<2:
        return None
    return statistics.correlation(x,y)


def report():
    with sqlite3.connect(DB_PATH.resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory = sqlite3.Row
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='phase4r_sources'").fetchone():
            return {'status':'Awaiting migration / Phase 4b completion'}
        sources = [json.loads(r[0]) for r in c.execute('SELECT source_json FROM phase4r_sources')]
        runs = [dict(r) for r in c.execute('SELECT * FROM phase4r_runs')]
        live = {r['id']: dict(r) for r in c.execute('SELECT * FROM volatility_predictions')}
        phase5 = [dict(r) for r in c.execute('SELECT * FROM phase5_predictions ORDER BY target_candle_time')]
        transitions = [dict(r) for r in c.execute('SELECT * FROM experiment_transitions')]
    diagnostics = []
    for source in sources:
        complete = [r for r in runs if r['source_id']==source['id'] and r['status']=='complete']
        d = {'source_id':source['id'],'predictor':source['predictor'], 'target':source['target_candle_time'],'completed':len(complete)}
        if complete:
            columns = [[r[k] for r in complete] for k in ('p_quiet','p_normal','p_active')]
            d.update(mean=[statistics.mean(v) for v in columns],
                     sd=[statistics.stdev(v) if len(v)>1 else 0 for v in columns],
                     range=[max(v)-min(v) for v in columns])
            d['argmax_agreement'] = max(sum(r['argmax_class']==label for r in complete) for label in ('QUIET','NORMAL','ACTIVE'))/len(complete)
            d['dispersion'] = sum(v*v for v in d['sd'])
            actual = live[source['id']]['actual_class']
            if len(complete)==10 and actual:
                d.update(single_brier=brier([source[k] for k in ('p_quiet','p_normal','p_active')],actual), ensemble_brier=brier(d['mean'],actual), ensemble_error=int(winner(d['mean'])!=actual), single_error=int(winner([source[k] for k in ('p_quiet','p_normal','p_active')])!=actual))
        diagnostics.append(d)
    timestamps = {model:{s['target_candle_time'] for s in sources if s['predictor']==model} for model in ('openai','jev')}
    common = timestamps['openai'] & timestamps['jev']
    summaries = {}
    for model in ('openai','jev'):
        ds = [d for d in diagnostics if d['predictor']==model]
        scored = [d for d in ds if 'ensemble_brier' in d]
        def mean(key):
            return statistics.mean(d[key] for d in scored) if scored else None
        summaries[model] = {'n':len(ds),'expected_runs':len(ds)*10,'completed_runs':sum(d['completed'] for d in ds),'evaluated_n':len(scored),'single_brier':mean('single_brier'),'ensemble_brier':mean('ensemble_brier'),'mean_dispersion':mean('dispersion'),'dispersion_vs_ensemble_brier_r':correlation([d['dispersion'] for d in scored],[d['ensemble_brier'] for d in scored]),'dispersion_vs_single_error_r':correlation([d['dispersion'] for d in scored],[d['single_error'] for d in scored])}
    paired_targets = {d['target'] for d in diagnostics if 'ensemble_brier' in d and d['predictor']=='openai'} & {d['target'] for d in diagnostics if 'ensemble_brier' in d and d['predictor']=='jev'}
    paired = {model: {key:statistics.mean(d[key] for d in diagnostics if d['predictor']==model and d['target'] in paired_targets) if paired_targets else None for key in ('single_brier','ensemble_brier')} for model in ('openai','jev')}
    evaluated = [r for r in phase5 if r['evaluated_at'] is not None]
    return {'transitions':transitions,'phase4r':summaries,'paired_n':len(common),'paired_evaluated_n':len(paired_targets),'paired_scores':paired,'diagnostics':diagnostics,'failed_or_uncertain_runs':[r for r in runs if r['status']!='complete'],'phase5':{'predicted_n':sum(r['status']=='complete' for r in phase5),'attempted_n':len(phase5),'model':metrics(evaluated,'predicted_rv'),'previous_hour_baseline':metrics(evaluated,'previous_rv'),'predicted_vs_actual':evaluated,'pending':[r for r in phase5 if r['evaluated_at'] is None]}}


def page(analysis=False):
    data = report()
    def table(items):
        if not items:
            return '<p>Pending</p>'
        keys = list(items[0])
        esc = lambda v: html.escape(str(v))
        return '<div style="overflow:auto"><table border="1"><tr>'+''.join('<th>'+esc(k)+'</th>' for k in keys)+'</tr>'+''.join('<tr>'+''.join('<td>'+esc(row.get(k,''))+'</td>' for k in keys)+'</tr>' for row in items)+'</table></div>'
    content = '<h1>Phase 4R / Phase 5</h1><p>SD is sample SD. Agreement is modal argmax frequency. Only complete 10-run ensembles and completed target windows enter scores. All paired scores use the same common evaluated timestamps.</p>'
    if 'phase4r' in data:
        content += '<h2>Phase 4R</h2>'+table([dict(predictor=k,**v) for k,v in data['phase4r'].items()])
        content += '<p>Paired n: '+str(data['paired_n'])+'; paired evaluated n: '+str(data['paired_evaluated_n'])+'</p>'+table([dict(predictor=k,**v) for k,v in data['paired_scores'].items()])
        content += '<h2>Phase 5</h2><p>Predicted N: '+str(data['phase5']['predicted_n'])+' / 96; attempted N: '+str(data['phase5']['attempted_n'])+'</p>'+table([dict(predictor=k,**data['phase5'][k]) for k in ('model','previous_hour_baseline')])
        if analysis:
            content += '<h2>Per-snapshot diagnostics</h2>'+table(data['diagnostics'])
            content += '<h2>Predicted vs actual RV (%)</h2>'+table([{k:r[k] for k in ('target_candle_time','predicted_rv','actual_rv','previous_rv','model_version')} for r in data['phase5']['predicted_vs_actual']])
        content += '<h2>Phase 5 pending / failed attempts</h2>'+table([{k:r[k] for k in ('target_candle_time','status','predicted_rv','error')} for r in data['phase5']['pending']])
        content += '<h2>Audit / pending / failures</h2><pre>'+html.escape(json.dumps({k:v for k,v in data.items() if k in ('transitions','failed_or_uncertain_runs')},indent=2))+'</pre>'
    else:
        content += html.escape(data['status'])
    return '<!doctype html><html><meta charset="utf-8"><title>BTC Phase 4R / 5</title><nav><a href="/">Phase 4</a> | <a href="/next-stages/">4R / 5 dashboard</a> | <a href="/analyze/?phase=phase4r">4R / 5 analysis</a> | <a href="/direction/">Phase 1–3</a></nav>'+content+'</html>'
