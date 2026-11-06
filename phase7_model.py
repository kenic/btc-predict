"""Frozen one-split selector. Historical predictions remain out-of-fit inputs."""
import itertools
import math
import statistics
from phase6_methods import LOGISTIC_KEYS, output

ACTIONS = ('gpt', 'order_flow', 'logistic', 'cash')
FEATURES = ('abs_return_1h', 'flow_strength', 'abs_sma_gap', 'gpt_confidence')
COSTS = (0, 2, 5, 10, 20)

def features(f, gpt):
    p = gpt.get('p_up')
    return dict(abs_return_1h=abs(f['return_1h']), flow_strength=abs(f['buy_ratio']-.5),
                abs_sma_gap=abs(f['sma_gap']), gpt_confidence=abs(p-.5) if p is not None else None)

def choose(x, model):
    key = model['feature']
    if key is not None and (x.get(key) is None or not math.isfinite(x[key])):
        return 'cash'
    return model['actions'][int(x[key] > model['threshold'])] if key else model['actions'][0]

def fit(rows, key, quantile):
    values = sorted(r['x'][key] for r in rows) if key else []
    if key and any(v is None or not math.isfinite(v) for v in values):
        raise ValueError('Incomplete selector training features')
    threshold = values[int((len(values)-1)*quantile)] if key else None
    groups = [[r for r in rows if r['x'][key] <= threshold],
              [r for r in rows if r['x'][key] > threshold]] if key else [rows]
    if min(map(len, groups)) < 20:
        return None
    actions = [max(ACTIONS, key=lambda m: sum(r['net'][m] for r in g)) for g in groups]
    return dict(feature=key, quantile=quantile, threshold=threshold, actions=actions,
                leaf_n=list(map(len, groups)))

def train(rows):
    if len(rows) != 200 or len({r['target'] for r in rows}) != 200:
        raise ValueError('Exactly 200 distinct historical accepted targets required')
    rows = sorted(rows, key=lambda r:r['target'])
    specs = [(None, .5)] + list(itertools.product(FEATURES, (.25, .5, .75)))
    candidates = []
    for key, q in specs:
        scores = []; folds = []
        for start, end in ((80, 120), (120, 160), (160, 200)):
            cutoff = rows[start]['target']
            prior = [r for r in rows[:start] if r['available_at'] < cutoff and r['target']+3600 < cutoff]
            rule = fit(prior, key, q)
            if rule is None:
                # Do not compare candidates evaluated on different validation sets.
                scores = []; break
            vv = [r['net'][choose(r['x'], rule)] for r in rows[start:end]]
            scores.extend(vv); folds.append(dict(train_n=len(prior), validation_n=len(vv), mean_net_bps=statistics.mean(vv)))
        if scores:
            candidates.append(dict(feature=key, quantile=q, validation_n=len(scores),
                                   mean_net_bps=statistics.mean(scores), folds=folds))
    best = max(candidates, key=lambda r:r['mean_net_bps'])
    model = fit(rows, best['feature'], best['quantile'])
    fixed = max(ACTIONS, key=lambda m:sum(r['net'][m] for r in rows))
    return dict(selector=model, train_best_fixed=fixed, candidates=candidates,
                training_n=len(rows), objective='bid/ask net return, additional roundtrip 5bp',
                validation='three expanding chronological folds, 80/40,120/40,160/40; strict label cutoff',
                interpretation='all 6/6b development data; not an OOS performance claim')

def frozen_logistic(f, model):
    if any(f.get(k) is None or not math.isfinite(f[k]) for k in LOGISTIC_KEYS):
        return output(reason='Missing frozen logistic feature')
    z = model['coefficients'][0]
    for i,k in enumerate(LOGISTIC_KEYS):
        z += model['coefficients'][i+1]*(f[k]-model['mean'][i])/model['scale'][i]
    p = 1/(1+math.exp(-max(-40, min(40, z))))
    result = output(p_up=p, audit={'training_sha256':model['training_sha256'], 'frozen':True})
    result['version'] = 'phase7-frozen-logistic-v1'
    return result

def quote_pnl(direction, entry, exit_quote, cost=5):
    if direction is None:
        return 0.0
    for q in (entry, exit_quote):
        if not q or not all(math.isfinite(q[k]) and q[k]>0 for k in ('best_bid','best_ask')) or q['best_bid']>q['best_ask']:
            raise ValueError('Invalid quote')
    if direction == 'UP':
        return 10000*(exit_quote['best_bid']/entry['best_ask']-1)-cost
    if direction == 'DOWN':
        return 10000*(1-exit_quote['best_ask']/entry['best_bid'])-cost
    raise ValueError('Invalid direction')
