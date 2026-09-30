"""Phase 4 definitions. RV is percent, nonannualized, close-to-close log returns."""
import math
CLASSES = ("QUIET", "NORMAL", "ACTIVE")

def realized_volatility(candles, start):
    by_time = {}
    for candle in candles:
        t = int(candle[0])
        if t in by_time and by_time[t] != candle:
            raise ValueError("Conflicting candle timestamps")
        by_time[t] = candle
    times = list(range(start - 300, start + 3600, 300))
    if any(t not in by_time for t in times):
        raise ValueError("Need 13 consecutive 5m closes, including the preceding close")
    closes = [float(by_time[t][4]) for t in times]
    if any(not math.isfinite(c) or c <= 0 for c in closes):
        raise ValueError("Invalid close")
    return 100 * math.sqrt(math.fsum(math.log(b / a)**2 for a, b in zip(closes, closes[1:])))

def classify(rv, config):
    if not math.isfinite(rv) or rv < 0:
        raise ValueError("Invalid RV")
    return "QUIET" if rv < config["quiet_upper"] else "NORMAL" if rv < config["active_lower"] else "ACTIVE"

def probabilities(values):
    p = [float(v) for v in values]
    if len(p) != 3 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in p):
        raise ValueError("Invalid class probabilities")
    if not math.isclose(sum(p), 1, abs_tol=1e-6):
        raise ValueError("Probabilities must sum to one")
    return p

def winner(p):
    return CLASSES[max(range(3), key=lambda i: p[i])]

def brier(p, actual):
    return sum((v - int(c == actual))**2 for c, v in zip(CLASSES, probabilities(p)))

def quantile(values, q):
    values = sorted(values)
    x = (len(values) - 1) * q
    lo = int(x)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (x - lo)
