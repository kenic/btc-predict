"""Read-only exploratory Phase 5b/6 JOIN; no selector or trading decisions."""
import argparse
import csv
import json
from pathlib import Path
import sqlite3
import sys
from volatility import quantile

JOIN_SQL = '''SELECT v.target_candle_time,v.predicted_rv,v.actual_rv,v.status,
 e.gate,e.actual_return,g.status AS gpt_status,l.status AS logistic_status,
 g.direction AS gpt_direction,g.correct AS gpt_correct,g.signed_pnl AS gpt_pnl,
 l.direction AS logistic_direction,l.correct AS logistic_correct,l.signed_pnl AS logistic_pnl
 FROM phase5b_predictions v
 LEFT JOIN phase6_events e ON e.target=v.target_candle_time
 LEFT JOIN phase6_predictions g ON g.target=v.target_candle_time AND g.method='gpt'
 LEFT JOIN phase6_predictions l ON l.target=v.target_candle_time AND l.method='logistic'
 ORDER BY v.target_candle_time'''


def summarize(rows):
    valid = [r for r in rows if r['status'] == 'complete' and r['predicted_rv'] is not None]
    values = [r['predicted_rv'] for r in valid]
    if not values:
        return dict(percentile_population=0, bins=[])
    cuts = [quantile(values, q) for q in (.5, .75, .9)]
    def bucket(r):
        return sum(r['predicted_rv'] >= c for c in cuts)
    bins = []
    for i, label in enumerate(('0–50', '50–75', '75–90', '90–100')):
        group = [r for r in valid if bucket(r) == i]
        # Shared scored targets permit a fair GPT/logistic comparison. Flat
        # returns contribute zero PnL but are excluded from accuracy denominators.
        paired = [r for r in group if r['gpt_status'] == r['logistic_status'] == 'complete'
                  and r['gpt_pnl'] is not None and r['logistic_pnl'] is not None]
        result = dict(percentile=label, observations=len(group), paired_N=len(paired))
        for method in ('gpt', 'logistic'):
            scored = [r for r in group if r[method+'_status'] == 'complete' and r[method+'_pnl'] is not None]
            def metrics(items):
                outcomes = [r[method+'_correct'] for r in items if r[method+'_correct'] is not None]
                pnl = [r[method+'_pnl'] for r in items]
                return dict(N=len(items), accuracy_N=len(outcomes),
                            accuracy=sum(outcomes)/len(outcomes) if outcomes else None,
                            mean_signed_pnl=sum(pnl)/len(pnl) if pnl else None,
                            total_pnl=sum(pnl) if pnl else None)
            result[method] = dict(available=metrics(scored), paired=metrics(paired))
        bins.append(result)
    return dict(percentile_population=len(valid), cut_values=cuts,
                ties='Equal cut values go to the higher bin; bins may be empty',
                pnl_units='percent; unit long/short; no fees', bins=bins)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', type=Path, default=Path(__file__).resolve().parent/'btc.db')
    p.add_argument('--csv', action='store_true', help='Export all scheduled rows and joined outcomes')
    args = p.parse_args()
    with sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(JOIN_SQL)]
        config = dict(c.execute('SELECT * FROM phase5b_config').fetchone())
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=rows[0].keys() if rows else ['target_candle_time'])
        writer.writeheader()
        writer.writerows(rows)
    else:
        report = summarize(rows)
        report.update(scheduled_rows=len(rows), planned_rows=500,
                      start_target=config['start_target'], end_target=config['end_target'],
                      exploratory=True, window_recorded=len(rows) == 500,
                      warning='Phase 6 outcomes are gate-selected; percentiles describe all valid 5b predictions, not just accepted gates.')
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
