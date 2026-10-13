"""Read-only retrospective ACTIVE detectors; never imported by live workers."""
import json
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from phase6_methods import LOGISTIC_KEYS
from phase6 import aligned_features
from volatility import CLASSES, classify, probabilities, winner

METHODS = ('GPT', 'Logistic A — Multinomial', 'Logistic B — Binary')


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        # Ambiguous legacy local times cannot prove label availability.
        return parsed.timestamp() if parsed.tzinfo else math.inf
    except (ValueError, TypeError):
        return math.inf


def complete(features):
    return all(isinstance(features.get(k), (int, float)) and
               not isinstance(features[k], bool) and math.isfinite(features[k]) for k in LOGISTIC_KEYS)


def fit(features, rows, cutoff, binary=False):
    import numpy as np
    labels = (0, 1) if binary else CLASSES
    prior = [r for r in rows if r['target']+3600 < cutoff and
             r['available_at'] < cutoff and r['actual'] in CLASSES and complete(r['features'])]
    audit = dict(targets=[r['target'] for r in prior], n=len(prior), cutoff=cutoff)
    y = [int(r['actual']=='ACTIVE') if binary else r['actual'] for r in prior]
    if not complete(features) or len(prior)<100 or any(y.count(label)<10 for label in labels):
        return None, audit
    X = np.array([[r['features'][k] for k in LOGISTIC_KEYS] for r in prior])
    mean, scale = X.mean(0), X.std(0)
    scale[scale==0] = 1
    X = np.column_stack((np.ones(len(X)), (X-mean)/scale))
    x = np.r_[1, (np.array([features[k] for k in LOGISTIC_KEYS])-mean)/scale]
    if binary:
        w = np.zeros(X.shape[1])
        for _ in range(400):
            p = 1/(1+np.exp(-np.clip(X@w,-40,40)))
            penalty = .01*w; penalty[0] = 0
            w -= .1*(X.T@(p-np.array(y))/len(X)+penalty)
        p = float(1/(1+np.exp(-np.clip(x@w,-40,40))))
        return dict(active=p>=.5, probability=p), audit
    Y = np.array([[int(label==v) for label in labels] for v in y])
    w = np.zeros((X.shape[1],3))
    for _ in range(400):
        z = X@w; z -= z.max(axis=1,keepdims=True)
        p = np.exp(z); p /= p.sum(axis=1,keepdims=True)
        penalty = .01*w; penalty[0] = 0
        w -= .1*(X.T@(p-Y)/len(X)+penalty)
    z = x@w; p = np.exp(z-z.max()); p /= p.sum()
    return dict(active=winner(p)=='ACTIVE', probability=float(p[2])), audit


def metrics(rows):
    counts = {key:0 for key in ('TP','FP','FN','TN')}
    briers = []
    for actual, prediction in rows:
        counts[('TP' if actual else 'FP') if prediction['active'] else ('FN' if actual else 'TN')] += 1
        p = prediction.get('probability')
        if isinstance(p,(int,float)) and not isinstance(p,bool) and math.isfinite(p) and 0<=p<=1:
            briers.append((p-int(actual))**2)
    tp,fp,fn,tn = (counts[k] for k in ('TP','FP','FN','TN'))
    return dict(counts, recall=tp/(tp+fn) if tp+fn else None,
                precision=tp/(tp+fp) if tp+fp else None,
                accuracy=(tp+tn)/len(rows) if rows else None,
                brier=sum(briers)/len(briers) if briers else None, brier_n=len(briers))


def compare(rows):
    predictions = {method:{} for method in METHODS}
    actuals = {r['target']:r['actual']=='ACTIVE' for r in rows if r['actual'] in CLASSES}
    audits = {}
    for row in rows:
        t = row['target']
        if row.get('gpt') is not None:
            predictions[METHODS[0]][t] = row['gpt']
        for method,binary in zip(METHODS[1:],(False,True)):
            prediction,audit = fit(row['features'],rows,t,binary)
            audits.setdefault(t,{})[method] = audit
            if prediction is not None:
                predictions[method][t] = prediction
    common = sorted(set(actuals).intersection(*(set(v) for v in predictions.values())))
    return dict(common_n=len(common), actual_active=sum(actuals[t] for t in common),
                timestamps=common, methods={m:metrics([(actuals[t],predictions[m][t]) for t in common]) for m in METHODS},
                audit=audits, source_n=len(rows))


def report(path):
    from phase6_views import actual_volatility
    rows, candidates = [], {}
    try:
        with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
            c.row_factory = sqlite3.Row
            c.execute('BEGIN')
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            config = None
            if 'phase6_config' in tables:
                state = c.execute('SELECT config_json FROM phase6_config').fetchone()
                if state:
                    config = json.loads(state[0])['gate']['volatility_config']
            sources = []
            if {'volatility_predictions','phase4_configs'} <= tables:
                sources = [dict(r) for r in c.execute("SELECT p.*,cfg.config_json FROM volatility_predictions p JOIN phase4_configs cfg USING(config_id) WHERE predictor='openai' ORDER BY target_candle_time")]
                if config is None and sources:
                    config = json.loads(sources[0]['config_json'])
            if config is None:
                return dict(compare([]), status='Frozen Phase 4 configuration unavailable')
            for r in sources:
                if json.loads(r['config_json']) != config:
                    continue
                try:
                    p = probabilities([r['p_quiet'],r['p_normal'],r['p_active']])
                    candidates[r['target_candle_time']] = dict(target=r['target_candle_time'], context=r['context'], gpt=dict(active=winner(p)=='ACTIVE',probability=p[2]))
                except (ValueError,TypeError):
                    continue
            if 'phase6_events' in tables:
                for r in map(dict,c.execute('SELECT * FROM phase6_events ORDER BY target')):
                    if r['target'] in candidates:
                        continue  # Cached Phase 4 single shot is the same forecast.
                    gpt = None
                    if r['single_class'] in CLASSES:
                        gpt = dict(active=r['single_class']=='ACTIVE',probability=None)
                        if 'phase6_calls' in tables:
                            call = c.execute("SELECT probabilities_json FROM phase6_calls WHERE target=? AND slot=0 AND status='complete'",(r['target'],)).fetchone()
                            try:
                                if call: gpt['probability'] = probabilities(json.loads(call[0]))[2]
                            except (ValueError,TypeError):
                                pass
                    candidates[r['target']] = dict(target=r['target'],context=r['context'],gpt=gpt)
            # Existing Phase 2/3 snapshots can supply training features, never direction GPT labels.
            if 'predictions' in tables:
                for r in map(dict,c.execute("SELECT * FROM predictions WHERE phase IN ('phase2','phase3') AND predictor='openai' ORDER BY id")):
                    candidates.setdefault(r['target_candle_time'],dict(target=r['target_candle_time'],context=r['context'],gpt=None))
            outcomes = actual_volatility(list(candidates.values()),c,tables,config,time.time(),Path(path).resolve().parent)
            availability = {}
            for table in ('volatility_predictions','phase5_predictions'):
                if table in tables:
                    for r in c.execute(f'SELECT target_candle_time,actual_rv,evaluated_at FROM {table} WHERE actual_rv IS NOT NULL AND evaluated_at IS NOT NULL'):
                        availability.setdefault(r[0],[]).append(timestamp(r[2]))
            for t,r in sorted(candidates.items()):
                try:
                    f = aligned_features(r['context'], t)
                except (ValueError,TypeError,IndexError,KeyError,ZeroDivisionError):
                    f = {}
                actual = outcomes[t]['actual_regime']
                # Archives have no historical availability proof: usable as test truth,
                # but never silently assume their labels existed at a past prediction.
                rows.append(dict(r,features=f,actual=actual,
                                 available_at=max(availability[t]) if t in availability else math.inf))
    except sqlite3.OperationalError:
        return dict(compare([]),status='Experiment database unavailable')
    return dict(compare(rows),status='Retrospective / exploratory; analysis-only')


def render(path):
    from ui import card, styled_table, metric_blocks
    from html import escape
    data = report(path)
    def pct(v): return '—' if v is None else f'{v:.1%}'
    body = '<p>'+escape(data['status'])+'</p>'+metric_blocks([
        ('Common timestamps N',data['common_n']),('Actual ACTIVE',data['actual_active'])])
    body += styled_table(['Method','Missed ACTIVE FN / actual ACTIVE','ACTIVE Recall','False ACTIVE FP / predicted ACTIVE','ACTIVE Precision','Detected ACTIVE TP / actual ACTIVE','TN','Accuracy','Binary Brier','Brier N'],[
        [m,f"{s['FN']} / {data['actual_active']}",pct(s['recall']),f"{s['FP']} / {s['TP']+s['FP']}",pct(s['precision']),f"{s['TP']} / {data['actual_active']}",s['TN'],pct(s['accuracy']),'—' if s['brier'] is None else f"{s['brier']:.4f}",s['brier_n']] for m,s in data['methods'].items()])
    body += '<p>Missed ACTIVE = actual ACTIVE but predicted NOT ACTIVE. False ACTIVE = predicted ACTIVE but actual NOT ACTIVE. All methods are compared on identical common timestamps, including rejected gates. Logistic results are retrospective expanding-window estimates; no future labels are used for training. Frozen Phase 4 thresholds; no tuning. Brier uses available probabilities only; compare its stated N.</p><p>Exploratory results do not guarantee future prospective performance. No significance claim is made.</p>'
    if data['common_n']<100 or data['actual_active']<10:
        body += '<p><strong>Small N / few ACTIVE outcomes: evidence is insufficient for a reliable superiority claim.</strong></p>'
    if not data['common_n']:
        body += '<p>No common evaluable timestamps. Logistic requires 100 complete strictly prior labeled rows and 10 per class (all three classes for A; both classes for B). Archive-only labels without historical evaluation times cannot enter training.</p>'
    return card('ACTIVE detector comparison',body)
