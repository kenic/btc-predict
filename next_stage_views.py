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
        has_audit = c.execute("SELECT 1 FROM sqlite_master WHERE name='phase4r_retry_audit'").fetchone()
        retries = [dict(r) for r in c.execute('SELECT * FROM phase4r_retry_audit')] if has_audit else []
    diagnostics = []
    for source in sources:
        complete = [r for r in runs if r['source_id']==source['id'] and r['status']=='complete']
        invalid = sum(r['source_id']==source['id'] and r['status']=='invalid' for r in runs)
        d = {'source_id':source['id'],'predictor':source['predictor'], 'target':source['target_candle_time'],'completed':len(complete),'invalid':invalid,'processed':len(complete)+invalid}
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
        summaries[model] = {'n':len(ds),'expected_runs':len(ds)*10,'completed_runs':sum(d['completed'] for d in ds),'invalid_runs':sum(d['invalid'] for d in ds),'processed_runs':sum(d['processed'] for d in ds),'excluded_invalid_snapshots':sum(d['invalid']>0 for d in ds),'evaluated_n':len(scored),'single_brier':mean('single_brier'),'ensemble_brier':mean('ensemble_brier'),'mean_dispersion':mean('dispersion'),'dispersion_vs_ensemble_brier_r':correlation([d['dispersion'] for d in scored],[d['ensemble_brier'] for d in scored]),'dispersion_vs_single_error_r':correlation([d['dispersion'] for d in scored],[d['single_error'] for d in scored])}
    paired_targets = {d['target'] for d in diagnostics if 'ensemble_brier' in d and d['predictor']=='openai'} & {d['target'] for d in diagnostics if 'ensemble_brier' in d and d['predictor']=='jev'}
    paired = {model: {key:statistics.mean(d[key] for d in diagnostics if d['predictor']==model and d['target'] in paired_targets) if paired_targets else None for key in ('single_brier','ensemble_brier')} for model in ('openai','jev')}
    evaluated = [r for r in phase5 if r['evaluated_at'] is not None]
    return {'retry_exceptions':retries,'transitions':transitions,'phase4r':summaries,'paired_n':len(common),'paired_evaluated_n':len(paired_targets),'paired_scores':paired,'diagnostics':diagnostics,'failed_or_uncertain_runs':[r for r in runs if r['status']!='complete'],'phase5':{'predicted_n':sum(r['status']=='complete' for r in phase5),'attempted_n':len(phase5),'model':metrics(evaluated,'predicted_rv'),'previous_hour_baseline':metrics(evaluated,'previous_rv'),'predicted_vs_actual':evaluated,'pending':[r for r in phase5 if r['evaluated_at'] is None]}}


def page(analysis=False, section=None):
    from ui import page_shell, card, metric_blocks, records_table
    data = report()
    title = {'repeated':'Repeated sampling — Phase 4R', 'regression':'RV regression — Phase 5'}.get(section, 'Repeated / Regression — Phase 4R / 5')
    content = '<h1>'+('Analysis — ' if analysis else '')+title+'</h1>'
    if 'phase4r' not in data:
        content += '<p>'+html.escape(data['status'])+'</p>'
    else:
        if section != 'regression':
            content += '<p class="muted">SD is sample SD. Agreement is modal argmax frequency. Invalid responses are recorded and excluded. Only complete 10-valid-response ensembles and completed target windows enter scores. All paired scores use the same common evaluated timestamps.</p>'
            for model, values in data['phase4r'].items():
                content += card('GPT' if model == 'openai' else 'Jev', metric_blocks([
                    ('Model N', values['n']), ('Expected runs', values['expected_runs']),
                    ('Completed runs', values['completed_runs']), ('Invalid runs', values['invalid_runs']), ('Processed runs', values['processed_runs']), ('Excluded snapshots', values['excluded_invalid_snapshots']), ('Evaluated N',values['evaluated_n']),
                    ('Single-shot Brier',values['single_brier']), ('Ensemble Brier',values['ensemble_brier']),
                    ('Dispersion',values['mean_dispersion'])]))
            content += card('Paired comparison', metric_blocks([('Paired N',data['paired_n']),
                ('Paired evaluated N',data['paired_evaluated_n'])])+records_table([
                dict(predictor=k, **v) for k,v in data['paired_scores'].items()]))
            content += card('Repeated sampling diagnostics',records_table([dict(predictor=k,**v) for k,v in data['phase4r'].items()])+records_table(data['diagnostics']))
            content += card('Invalid / pending / failed / uncertain repeats',records_table(data['failed_or_uncertain_runs']))
        if section != 'repeated':
            values = data['phase5']
            content += card('Regression progress',metric_blocks([('Predicted N',values['predicted_n']),('Target N',96),('Attempted N',values['attempted_n'])]))
            content += '<p class="muted">RV and errors are in percent units, not annualized. GPT and the previous-hour RV baseline use identical evaluated target rows. Bias = prediction − actual.</p>'
            for key,label in [('model','GPT'),('previous_hour_baseline','Previous-hour RV baseline')]:
                v=values[key]
                content += card(label,metric_blocks([('N',v['n']),('MAE',v['mae']),('RMSE',v['rmse']),('Bias',v['bias'])]))
            content += card('Predicted vs actual RV (%)',records_table([{k:r[k] for k in ('target_candle_time','predicted_rv','actual_rv','previous_rv','model_version')} for r in values['predicted_vs_actual']]))
            content += card('Pending / failed regression attempts',records_table([{k:r[k] for k in ('target_candle_time','status','predicted_rv','error')} for r in values['pending']]))
        content += card('Experiment audit',records_table(data['transitions']))
        content += card('Prior retry exceptions',records_table(data['retry_exceptions']))
    return page_shell(('Analysis — ' if analysis else '')+title,content,
                      'analysis' if analysis else section or 'repeated',
                      phase='phase5' if section == 'regression' else 'phase4r')
