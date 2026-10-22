"""Read-only Phase 6d dashboard. No initialization or provider requests."""
from html import escape
import sqlite3

from phase6d_analysis import report as analyze
from claude_views import utc,number,table_or_empty,direction_table
from ui import card,metric_blocks,page_shell


def report(source):
    try:
        return dict(status='ready',**analyze(source.with_name('phase6d.db'),source,source.with_name('claude-observation.db')))
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError):
        return dict(status='unavailable',message='Phase 6d has not started or data is currently unavailable.')


def detection_table(groups):
    return table_or_empty(('Population','N','TP','FN','FP','TN','ACTIVE recall (%)','ACTIVE precision (%)',
                           'Accuracy (%)','Brier N','Binary ACTIVE Brier'),
        [[name,s['n'],s['tp'],s['fn'],s['fp'],s['tn'],number(s['recall'],100),number(s['precision'],100),
          number(s['accuracy'],100),s['brier_n'],number(s['brier'])] for name,s in groups])


def page(source,analysis=False):
    data=report(source);title='Phase 6d — Claude independent ACTIVE gate'
    content='<h1>'+title+'</h1><p class="muted">Claude predicts volatility every hour and direction only on its own accepted ACTIVE targets. GPT acceptance is not required. No optional stopping.</p>'
    content+='<p><a href="/phase5c/">Phase 5c RV</a> · <a href="/phase6c/">Phase 6c GPT-selected direction</a></p>'
    if data['status']!='ready':
        content+='<p>'+escape(data['message'])+'</p>'
    else:
        content+=card('Independent observation window',metric_blocks([
            ('First target',utc(data['start_target'])),('End (exclusive)',utc(data['end_exclusive'])),
            ('Model',data['model']),('ACTIVE threshold (%)',data['thresholds']['active_lower']),('Recorded / planned',f"{data['recorded']} / {data['planned']}"),
            ('Claude accepted',data['gates'].get('accepted',0)),('Rejected',data['gates'].get('rejected',0)),
            ('Unknown / missing gate',data['gates'].get('unknown',0)),
            ('Actuals available',data['actuals_available']),('Actuals pending',data['actuals_pending'])]))
        content+='<p class="muted">Single ACTIVE, ten valid volatility repeats, modal ACTIVE and at least 8/10 ACTIVE votes. Same stored input and frozen thresholds as the existing experiment. Each accepted target receives one direction forecast.</p>'
        content+=card('Claude ACTIVE detection',detection_table([
            ('Single-shot class',data['active_detection']),('Accepted gate',data['gate_detection'])])+
            metric_blocks([('Three-class accuracy (%)',None if data['class_accuracy'] is None else data['class_accuracy']*100),
                           ('Three-class Brier',data['class_brier'])]))
        content+='<p class="muted">Single-shot detection uses valid scored classifications; gate detection requires a completed valid gate. Failed/missing calls remain unknown and are excluded from scored N, with coverage reported separately.</p>'
        content+=card('Direction on Claude-accepted targets',direction_table([('Own accepted targets',{'claude':data['direction']})]))
        content+=card('GPT / Claude ACTIVE — matched targets and snapshots',detection_table(
            [(name,s) for name,s in data['class_comparison'].items()]))
        content+=card('Independent gate overlap',metric_blocks([
            ('Common comparable gates',data['gate_common_target_n']),
            ('Both accepted',data['gate_overlap'].get('both_accepted',0)),
            ('Claude only accepted',data['gate_overlap'].get('claude_only_accepted',0)),
            ('GPT only accepted',data['gate_overlap'].get('gpt_only_accepted',0)),
            ('Neither accepted',data['gate_overlap'].get('neither_accepted',0))]))
        content+=card('Direction — common scored targets across both gates',
            metric_blocks([('Common-target N',data['common_direction']['common_target_n'])])+
            direction_table([('Both gate populations',data['common_direction']['methods'])]))
        content+='<p class="muted">Common comparison requires GPT, Logistic, Claude 6c and Claude 6d with the same target, input and realized return. Different gate populations are not pooled as an accuracy comparison. FLAT is excluded from accuracy/Brier and has zero signed PnL; PnL is percent without fees.</p>'
        content+=card('GPT / Claude 6d same vs different',direction_table([
            (label,{m:group[m] for m in ('gpt','claude6d')}) for label,group in data['same_different'].items()]))
        content+=card('Coverage',metric_blocks([(key,value) for key,value in sorted(data['statuses'].items())]+
            [('Class '+key,value) for key,value in sorted(data['class_calls'].items())]+
            [('Direction '+key,value) for key,value in sorted(data['direction_calls'].items())]))
        rows=[[utc(r['target']),r['status'],r['single_class'] or '—',r['repeat_modal'] or '—',
               number(r['repeat_agreement'],100),r['gate'],number(r['actual_rv']),r['actual_class'] or 'Pending',r['direction_status'],
               r['direction'] or '—',number(r['p_up']),r['actual_direction'] or 'Pending',number(r['brier']),
               number(r['signed_pnl']),r['reason'] or '—'] for r in data['recent']]
        content+=card('Latest 100 target hours',table_or_empty(
            ('Target (UTC)','Status','Single class','Repeat modal','Agreement (%)','Gate','Actual RV (%)',
             'Actual class','Direction status','Claude direction','p(UP)','Actual direction','Brier','Signed PnL (%)','Reason'),rows,
            'Awaiting the first scheduled target.'))
    return page_shell(title,content,'analysis' if analysis else 'phase6d',phase='phase6d' if analysis else None,refresh=60)
