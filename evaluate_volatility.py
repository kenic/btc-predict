"""Evaluate only complete exact 5m windows; gaps leave predictions pending."""
import json
import sqlite3
from datetime import datetime, timezone
from phase4 import DB_PATH, now
from prepare_phase4 import fetch_candles
from volatility import realized_volatility, classify, brier, winner

def evaluate_all():
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory=sqlite3.Row
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='volatility_predictions'").fetchone():
            return
        rows=c.execute('SELECT p.*,cfg.config_json FROM volatility_predictions p JOIN phase4_configs cfg USING(config_id) WHERE evaluated_at IS NULL ORDER BY target_candle_time').fetchall()
    failures=0
    for row in rows:
        t=row['target_candle_time']
        if datetime.now(timezone.utc).timestamp()<t+3600:
            continue
        try:
            candles=fetch_candles(t-300,t+3600)
            rv=realized_volatility(candles,t)
            label=classify(rv,json.loads(row['config_json']))
            p=[row['p_quiet'],row['p_normal'],row['p_active']]
            with sqlite3.connect(DB_PATH) as c:
                c.execute('UPDATE volatility_predictions SET actual_rv=?,actual_class=?,correct=?,brier=?,evaluated_at=? WHERE id=? AND evaluated_at IS NULL',
                          (rv,label,int(winner(p)==label),brier(p,label),now(),row['id']))
            print(f"Phase 4 id={row['id']} RV={rv:.6f}% {label}")
        except Exception as exc:
            failures+=1
            print(f"Phase 4 id={row['id']} left pending: {exc}")
    if failures:
        raise RuntimeError(f'{failures} Phase 4 evaluations failed')

if __name__=='__main__':
    evaluate_all()
