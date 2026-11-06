"""Additive read-only presentation; frozen Phase7 report/runtime remain unchanged."""
import json
from pathlib import Path
import sqlite3
import time
from phase7_report import report as frozen_report, LABELS
from volatility import realized_volatility, classify, probabilities, winner


def report(path, clock=None, final_ci=True):
    clock=time.time() if clock is None else clock
    result=frozen_report(path,clock=clock,final_ci=final_ci)
    if not result.get('recent'):
        return result
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        c.execute('BEGIN')
        config=json.loads(c.execute('SELECT arm_json FROM config').fetchone()[0])
        targets=[e['target'] for e in result['recent']]
        calls={r['target']:dict(r) for r in c.execute("SELECT target,status,payload_json FROM calls WHERE kind='class' AND slot=0 AND target BETWEEN ? AND ?",(min(targets),max(targets)))}
    for event in result['recent']:
        event.update(volatility_prediction=None,actual_volatility=None,actual_rv=None,
                     actual_volatility_status='pending' if clock<event['target']+3660 else 'unavailable')
        call=calls.get(event['target'])
        if call and call['status']=='complete':
            try:
                event['volatility_prediction']=winner(probabilities(json.loads(call['payload_json'])[0]))
            except (ValueError,TypeError,IndexError,KeyError):
                pass
    try:
        with sqlite3.connect(Path(config['candles']).resolve().as_uri()+'?mode=ro',uri=True) as c:
            c.row_factory=sqlite3.Row
            c.execute('BEGIN')
            for event in result['recent']:
                if event['actual_volatility_status']=='pending':continue
                t=event['target']
                rows=list(c.execute('SELECT start_ts,close FROM candles_5m WHERE start_ts BETWEEN ? AND ? ORDER BY start_ts',(t-300,t+3300)))
                try:
                    rv=realized_volatility([[r['start_ts'],0,0,0,r['close']] for r in rows],t)
                    label=classify(rv,config['phase6_design']['gate']['volatility_config'])
                except (ValueError,TypeError,KeyError):
                    continue
                event.update(actual_rv=rv,actual_volatility=label,actual_volatility_status='complete')
    except (sqlite3.Error,OSError,KeyError):
        # Never fetch/backfill market data or create a missing archive from a view.
        pass
    return result


def render(path):
    from ui import page_shell,card,metric_blocks,styled_table
    from datetime import datetime,timezone
    from html import escape
    r=report(path);content='<h1>Phase 7 · Frozen model selector</h1><p>'+escape(r['status'])+'</p>'
    content+='<p>60 days · Paper execution only · Bid/ask spread plus 5 bp round-trip cost. No live trading.</p>'
    if r.get('start_target'):
        fmt=lambda t:datetime.fromtimestamp(t,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        content+=card('Fixed window','<p>'+fmt(r['start_target'])+' → '+fmt(r['end_exclusive'])+' (exclusive)</p>')
        content+=card('Coverage',metric_blocks([('Recorded hours',r['recorded_hours']),('Accepted',r['accepted']),('Executable',r['executable_n']),('Missing hours',r['missing_hours'])]))
        train=r['training'];content+=card('Frozen selection rule','<pre>'+escape(json.dumps(train['selector'],indent=2))+'</pre><p>Training: 200 accepted Phase 6 + 6b targets. Logistic fit: '+str(train['logistic_n'])+' labeled snapshots.</p>')
        rows=[]
        for m,v in r['methods'].items():rows.append([LABELS[m],v['n'],v['trades'],'—' if v['mean_net_bps'] is None else f"{v['mean_net_bps']:.3f}",f"{v['max_drawdown_bps']:.2f}"])
        content+=card('Provisional paper results',styled_table(('Method','Opportunities','Trades','Mean net bp','Max DD bp'),rows))
        content+=card('Primary comparison','<p>'+escape(r['primary']['verdict'])+'</p><p>Selector − Order Flow. Confidence interval evaluated only after the fixed end. Missing execution outcomes prevent a positive final verdict.</p>')
        content+=card('Recent hours','<p>Forecast: initial GPT volatility class. Actual: realized volatility from completed 5-minute closes, using the frozen class thresholds. Pending = hour not yet settled; Unavailable = incomplete candle data.</p>'+styled_table(('Target UTC','Volatility forecast','Actual volatility','Actual RV (%)','Gate','Selection','Execution'),[[fmt(e['target']),e['volatility_prediction'] or 'Unavailable',e['actual_volatility'] or e['actual_volatility_status'].capitalize(),'—' if e['actual_rv'] is None else f"{e['actual_rv']:.4f}",e['gate'],e['selection'],e['execution_status']] for e in r['recent']]))
    content+='<p><a href="/phase7/data.json">Full report JSON</a></p>'
    return page_shell('Phase 7',content,'phase7',refresh=60)
