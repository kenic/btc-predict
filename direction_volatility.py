"""Read-only, retrospective direction outcomes conditioned on future RV."""
import json
import math
import sqlite3
import statistics
from pathlib import Path
from volatility import realized_volatility, classify, CLASSES

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'direction_history_5m.json'


def load_candles():
    candles = {}
    for path in (ROOT / 'phase4_history_5m.json', CACHE):
        if path.exists():
            for candle in json.loads(path.read_text()):
                t = int(candle[0])
                if t in candles and candles[t] != candle:
                    raise ValueError('Conflicting historical candles')
                candles[t] = candle
    return candles


def assign_rv(rows, candles, config):
    assigned, missing = [], 0
    outcomes = {}
    for row in rows:
        t = row['target_candle_time']
        if t not in outcomes:
            try:
                window = [candles[x] for x in range(t - 300, t + 3600, 300)]
                outcomes[t] = realized_volatility(window, t)
            except (KeyError, ValueError, TypeError):
                outcomes[t] = None
        rv = outcomes[t]
        if rv is None:
            missing += 1
            continue
        assigned.append(dict(row, rv=rv, regime=classify(rv, config)))
    return assigned, missing


def summarize(rows):
    n = len(rows)
    returns = [abs(r['actual_return']) for r in rows
               if r['actual_return'] is not None and math.isfinite(r['actual_return'])]
    return dict(n=n, correct=sum(r['correct'] for r in rows),
                accuracy=sum(r['correct'] for r in rows)/n if n else None,
                brier=statistics.mean(r['brier'] for r in rows) if n else None,
                return_n=len(returns), mean_return=statistics.mean(returns) if returns else None,
                median_return=statistics.median(returns) if returns else None,
                mean_rv=statistics.mean(r['rv'] for r in rows) if n else None)


def report(db_path, phase='all'):
    config = json.loads((ROOT / 'phase4_config.json').read_text())
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        raw = c.execute("""SELECT * FROM predictions WHERE phase IN ('phase1','phase2','phase3')
          AND predictor IN ('openai','jev') AND actual_direction IN ('UP','DOWN')
          AND evaluated_at IS NOT NULL""").fetchall()
    rows, invalid = [], 0
    for raw_row in raw:
        r = dict(raw_row)
        if phase != 'all' and r['phase'] != phase:
            continue
        up, down = r['p_up'], r['p_down']
        if (up is None or down is None or not math.isfinite(up) or not math.isfinite(down)
            or not 0 <= up <= 1 or not 0 <= down <= 1 or not math.isclose(up+down, 1, abs_tol=1e-6)):
            invalid += 1
            continue
        r['correct'] = r['correct'] if r['correct'] in (0,1) else int(('UP' if up >= down else 'DOWN') == r['actual_direction'])
        r['brier'] = (up-int(r['actual_direction']=='UP'))**2
        rows.append(r)
    assigned, missing = assign_rv(rows, load_candles(), config)
    cohorts = ['all','phase1','phase2','phase3'] if phase == 'all' else [phase]
    groups = []
    coverage = []
    for p in cohorts:
        for model in ('openai','jev'):
            selected = [r for r in assigned if r['predictor']==model and (p=='all' or r['phase']==p)]
            eligible = [r for r in rows if r['predictor']==model and (p=='all' or r['phase']==p)]
            coverage.append((p, model, len(eligible), len(selected), len(eligible)-len(selected)))
            for regime in ('ALL',)+CLASSES+('NON-ACTIVE',):
                subset = [r for r in selected if regime=='ALL' or r['regime']==regime or (regime=='NON-ACTIVE' and r['regime']!='ACTIVE')]
                groups.append((p,model,regime,summarize(subset)))
    return dict(groups=groups,coverage=coverage,missing=missing,invalid=invalid,config=config)


if __name__ == '__main__':
    # Explicit optional gap filling; never runs from a web request or experiment worker.
    import argparse
    from prepare_phase4 import fetch_candles
    parser = argparse.ArgumentParser(description='Fetch missing exact historical closes into an isolated JSON cache; predictions remain read-only.')
    parser.add_argument('--fill-gaps', action='store_true', required=True)
    parser.add_argument('--db', type=Path, default=ROOT/'btc.db')
    args = parser.parse_args()
    candles = load_candles()
    with sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro',uri=True) as c:
        targets = [r[0] for r in c.execute("SELECT DISTINCT target_candle_time FROM predictions WHERE phase IN ('phase1','phase2','phase3') AND evaluated_at IS NOT NULL")]
    failures = []
    for t in sorted(targets):
        if all(x in candles for x in range(t-300,t+3600,300)):
            continue
        try:
            fetched = fetch_candles(t-300,t+3600)
            realized_volatility(fetched,t)
            for candle in fetched:
                x = int(candle[0])
                if x in candles and candles[x] != candle:
                    raise ValueError('Conflicting historical candle')
            candles.update({int(x[0]):x for x in fetched if t-300 <= int(x[0]) < t+3600})
        except Exception as exc:
            failures.append((t,str(exc)))
    temp = CACHE.with_suffix('.tmp')
    temp.write_text(json.dumps(sorted(candles.values()),separators=(',',':'))+'\n')
    temp.replace(CACHE)
    print(json.dumps(dict(failed_windows=failures,report=report(args.db)),indent=2))


def render(data, table, stat):
    def pct(x):
        return f'{x:.4f}%' if x is not None else '—'
    body = table(['Phase','Model','Actual regime','N','Correct','Accuracy','Brier',
                  'Return N','Mean |return|','Median |return|','Mean RV'],
                 [(p,'GPT / OpenAI' if m=='openai' else 'Jev',reg,s['n'],s['correct'],
                   stat(s['accuracy'],'percent'),stat(s['brier'],'brier'),s['return_n'],
                   pct(s['mean_return']),pct(s['median_return']),pct(s['mean_rv']))
                  for p,m,reg,s in data['groups']])
    coverage = table(['Phase','Model','Eligible N','RV assigned','Missing RV'],data['coverage'])
    conclusions = []
    cohort = data['groups'][0][0]
    for model,label in [('openai','GPT / OpenAI'),('jev','Jev')]:
        groups = {reg:s for p,m,reg,s in data['groups'] if p==cohort and m==model}
        a,b = groups['ACTIVE'],groups['NON-ACTIVE']
        if a['n'] and b['n']:
            conclusions.append(f"{label}: ACTIVE {a['accuracy']:.1%} (N={a['n']}), non-ACTIVE {b['accuracy']:.1%} (N={b['n']}); difference {100*(a['accuracy']-b['accuracy']):+.1f} percentage points.")
        else:
            conclusions.append(f"{label}: insufficient observations to compare ACTIVE / non-ACTIVE (N={a['n']} / {b['n']}).")
    cfg=data['config']
    return f'''<section><h2>Direction accuracy by realized volatility</h2>
<p class="muted">Retrospective / conditional on realized volatility: subsequent-hour RV is known only afterward.
This is exploratory and not tradable prospectively. Phase 4 thresholds were calibrated after the direction experiments.
Observations from different models for the same hour are not independent.</p>
<p>RV = 100 × sqrt(sum(log(C_i / C_(i−1))²)), exactly twelve consecutive 5m returns including the preceding close.
QUIET: RV &lt; {cfg['quiet_upper']:.9f}%; NORMAL: {cfg['quiet_upper']:.9f}% ≤ RV &lt; {cfg['active_lower']:.9f}%;
ACTIVE: RV ≥ {cfg['active_lower']:.9f}%.</p><p>{' '.join(conclusions)}</p>
<p class="muted">Missing RV excluded: {data['missing']}; invalid probability rows excluded: {data['invalid']}.
ALL includes only RV-assigned rows. Stored actual_return is in percent; missing/invalid returns are excluded only
from magnitude metrics (Return N). No interpolation or network fetching occurs in this page.</p>{coverage}{body}</section>'''
