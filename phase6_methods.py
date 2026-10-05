"""Frozen v1 direction rules operating only on the stored Phase 3 text input."""
import hashlib
import json
import math
import re

VERSION = 'phase6-v1'
BASE_METHODS = ('gpt', 'momentum_1h', 'momentum_short', 'reversal_1h',
                'always_up', 'breakout', 'trend', 'order_flow', 'order_book', 'logistic')
METHODS = BASE_METHODS + ('ensemble_vote', 'ensemble_probability')
LOGISTIC_KEYS = ('return_1h', 'return_5m', 'return_15m', 'sma_gap', 'macd_hist',
                 'buy_ratio', 'imbalance', 'micro_offset')


def extract(context):
    """Parse the exact saved, rounded representation; never query market.db here."""
    def number(pattern, text=context):
        match = re.search(pattern, text)
        if not match:
            raise ValueError('Missing snapshot field: '+pattern)
        value = float(match.group(1))
        if not math.isfinite(value):
            raise ValueError('Non-finite snapshot field')
        return value
    hour = context.split('=== 1-HOUR CONTEXT ===')[1].split('=== 15-MINUTE CONTEXT ===')[0]
    short = context.split('=== 5-MINUTE CONTEXT ===')[1].split('=== MARKET MICROSTRUCTURE ===')[0]
    book = context.split('--- ORDER BOOK: AVERAGES ---')[1].split('--- AGGRESSIVE TRADE FLOW ---')[0]
    flow = context.split('--- AGGRESSIVE TRADE FLOW ---')[1]
    reference = number(r'Reference close:\s*([\d.]+)')
    sma20 = number(r'SMA20:\s*([\d.]+)', hour)
    sma50 = number(r'SMA50:\s*([\d.]+)', hour)
    candles = re.findall(r'(\d\d:\d\d) O=([\d.]+) H=([\d.]+) L=([\d.]+) C=([\d.]+) V=([\d.]+)', short)
    features = {'reference': reference,
                'return_1h': number(r'1h:\s*([-+\d.]+)%', hour),
                'return_5m': number(r'5m:\s*([-+\d.]+)%', short),
                'return_15m': number(r'15m:\s*([-+\d.]+)%', short),
                'sma_gap': 100*(sma20/sma50-1),
                'macd_hist': number(r'Histogram:\s*([-+\d.]+)', hour)/reference*100}
    # Optional fields make their individual methods unavailable, not all methods.
    for key, pattern, text in (
            ('imbalance', r'60m:[^\n]*imb10=([-+\d.]+)', book),
            ('micro_offset', r'60m:[^\n]*micro_offset=([-+\d.]+)', book),
            ('buy_ratio', r'60m:[^\n]*buy_ratio=([-+\d.]+)', flow)):
        try:
            features[key] = number(pattern, text)
        except ValueError:
            features[key] = None
    if len(candles) == 12:
        features.update(breakout_close=float(candles[-1][4]),
                        range_high=max(float(x[2]) for x in candles[:-1]),
                        range_low=min(float(x[3]) for x in candles[:-1]))
    return features


def output(direction=None, p_up=None, reason=None, audit=None):
    if p_up is not None:
        if isinstance(p_up, bool) or not isinstance(p_up, (int, float)) or not math.isfinite(p_up) or not 0 <= p_up <= 1:
            raise ValueError('Invalid direction probability')
        direction = 'UP' if p_up >= .5 else 'DOWN'
    if direction not in (None, 'UP', 'DOWN'):
        raise ValueError('Invalid direction')
    return dict(status='complete' if direction else 'unavailable', direction=direction,
                p_up=p_up, p_down=None if p_up is None else 1-p_up, reason=reason,
                version=VERSION, audit=audit)


def sign(value):
    if value is None or not math.isfinite(value) or value == 0:
        return output(reason='Missing/non-finite/zero signal')
    return output('UP' if value > 0 else 'DOWN')


def rule(method, f):
    if method == 'always_up':
        return output('UP')
    if method == 'momentum_1h':
        return sign(f['return_1h'])
    if method == 'reversal_1h':
        return sign(-f['return_1h'])
    if method == 'momentum_short':
        return sign((f['return_5m']+f['return_15m'])/2)
    if method == 'trend':
        # Equal vote of SMA20/SMA50 and MACD histogram; disagreement abstains.
        return sign(sum((v > 0)-(v < 0) for v in (f['sma_gap'], f['macd_hist'])))
    if method == 'order_flow':
        return sign(None if f.get('buy_ratio') is None else f['buy_ratio']-.5)
    if method == 'order_book':
        if f.get('imbalance') is None or f.get('micro_offset') is None:
            return output(reason='Missing 60m imbalance/microprice')
        return sign(sum((v > 0)-(v < 0) for v in (f['imbalance']-.5, f['micro_offset'])))
    if method == 'breakout':
        if 'range_high' not in f:
            return output(reason='Need twelve completed 5m candles')
        value = 1 if f['breakout_close'] > f['range_high'] else -1 if f['breakout_close'] < f['range_low'] else 0
        return sign(value)
    raise ValueError('Unknown method: '+method)


def ensembles(results):
    eligible = {k: v for k, v in results.items() if k in BASE_METHODS and v['status'] == 'complete'}
    votes = [v['direction'] for v in eligible.values()]
    probabilities = [v['p_up'] for v in eligible.values() if v['p_up'] is not None]
    # Rule directions are deliberately not invented 0/1 probabilities.
    vote = sign(votes.count('UP')-votes.count('DOWN')) if len(votes) >= 3 else output(reason='Need >=3 eligible methods')
    average = output(p_up=sum(probabilities)/len(probabilities)) if len(probabilities) >= 2 else output(reason='Need >=2 probability methods')
    vote['audit'] = {'eligible': sorted(eligible)}
    average['audit'] = {'eligible': sorted(k for k,v in eligible.items() if v['p_up'] is not None)}
    return {'ensemble_vote': vote, 'ensemble_probability': average}


def logistic(features, rows, cutoff):
    """Expanding fit, strict closure and label availability before target boundary.

    NumPy-only standardized L2 logistic regression, deterministic 400 full-batch
    steps, learning rate .1, lambda .01; 100 complete rows, >=10 per class.
    All filtering/scaling/fitting occurs independently for each prediction.
    """
    import numpy as np
    eligible = []
    for row in sorted(rows, key=lambda r: (r['target'], r['id'])):
        if row['target']+3600 >= cutoff or row['available_at'] >= cutoff:
            continue
        if row['outcome'] not in ('UP', 'DOWN'):
            continue
        f = row['features']
        if any(f.get(k) is None or not math.isfinite(f[k]) for k in LOGISTIC_KEYS):
            continue
        eligible.append(row)
    audit = {'ids': [r['id'] for r in eligible], 'n': len(eligible), 'cutoff': cutoff}
    if len(eligible) < 100 or min(sum(r['outcome']==label for r in eligible) for label in ('UP','DOWN')) < 10:
        return output(reason='Need >=100 strictly prior labeled rows and >=10 per class', audit=audit)
    if any(features.get(k) is None or not math.isfinite(features[k]) for k in LOGISTIC_KEYS):
        return output(reason='Missing current logistic features', audit=audit)
    X = np.array([[r['features'][k] for k in LOGISTIC_KEYS] for r in eligible], dtype=float)
    y = np.array([r['outcome']=='UP' for r in eligible], dtype=float)
    mean, scale = X.mean(axis=0), X.std(axis=0)
    scale[scale == 0] = 1
    X = np.column_stack([np.ones(len(X)), (X-mean)/scale])
    w = np.zeros(X.shape[1])
    for _ in range(400):
        p = 1/(1+np.exp(-np.clip(X@w, -40, 40)))
        penalty = .01*w
        penalty[0] = 0
        w -= .1*(X.T@(p-y)/len(X)+penalty)
    x = np.r_[1, (np.array([features[k] for k in LOGISTIC_KEYS])-mean)/scale]
    p = float(1/(1+np.exp(-np.clip(x@w, -40, 40))))
    audit.update(keys=LOGISTIC_KEYS, mean=mean.tolist(), scale=scale.tolist(), coefficients=w.tolist(),
                 training_sha256=hashlib.sha256(json.dumps(eligible, sort_keys=True).encode()).hexdigest())
    return output(p_up=p, audit=audit)
