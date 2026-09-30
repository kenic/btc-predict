"""Explicit preparation only; never starts prediction or calls a model."""
import argparse
import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from phase4 import ROOT, DB_PATH, CONFIG_PATH, init_schema, assert_phase3_closed, config_hash
from volatility import realized_volatility, quantile, classify, CLASSES

def fetch_candles(start, end, granularity=300):
    params = urllib.parse.urlencode(dict(granularity=granularity,
        start=datetime.fromtimestamp(start, timezone.utc).isoformat(),
        end=datetime.fromtimestamp(end, timezone.utc).isoformat()))
    req=urllib.request.Request('https://api.exchange.coinbase.com/products/BTC-USD/candles?'+params,
                              headers={'User-Agent':'btc-predict-phase4/1.0'})
    with urllib.request.urlopen(req, timeout=30) as response:
        data=json.load(response)
    if not isinstance(data,list):
        raise ValueError('Unexpected candle response')
    return data

def prepare(freeze_only=False, close_only=False):
    if CONFIG_PATH.exists() and not close_only:
        raise RuntimeError('Existing frozen config will not be overwritten')
    backup=ROOT/'backups/phase3-boundary'
    backup.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory=sqlite3.Row
        rows=[] if freeze_only else c.execute("SELECT * FROM predictions WHERE phase='phase3' AND evaluated_at IS NULL").fetchall()
        for row in rows:
            t=row['target_candle_time']
            if int(datetime.now(timezone.utc).timestamp()) < t+3600:
                raise RuntimeError('Last Phase 3 target candle is not complete')
            candles=fetch_candles(t,t+7200,3600)
            candle=next((x for x in candles if int(x[0])==t),None)
            if candle is None:
                raise RuntimeError('Last Phase 3 candle unavailable')
            close=float(candle[4]); ref=row['candle_close']
            direction='UP' if close>ref else 'DOWN' if close<ref else 'FLAT'
            predicted='UP' if row['p_up']>=row['p_down'] else 'DOWN'
            c.execute('UPDATE predictions SET actual_close=?,actual_return=?,actual_direction=?,correct=?,evaluated_at=? WHERE id=? AND evaluated_at IS NULL',
                (close,100*(close/ref-1),direction,None if direction=='FLAT' else int(direction==predicted),datetime.now(timezone.utc).isoformat(),row['id']))
        if not freeze_only:
            assert_phase3_closed(c)
        c.commit()
        end=c.execute("SELECT max(target_candle_time)+3600 FROM predictions WHERE phase='phase3'").fetchone()[0]
        if not freeze_only:
            with sqlite3.connect(backup/'closed-btc.db') as dest:
                c.backup(dest)
    if not freeze_only:
        import subprocess
        expected=subprocess.check_output(['git','rev-parse','phase3-boundary-code^{commit}'],cwd=ROOT,text=True).strip()
        existing=subprocess.run(['git','rev-parse','--verify','phase3-complete^{commit}'],cwd=ROOT,text=True,capture_output=True)
        if existing.returncode == 0 and existing.stdout.strip()!=expected:
            raise RuntimeError('Existing completion tag differs from saved Phase 3 code')
        if existing.returncode != 0:
            subprocess.run(['git','tag','-a','phase3-complete','phase3-boundary-code^{commit}',
                            '-m','Phase 3 closed: 48 evaluated predictions per model; DB in backups/phase3-boundary/closed-btc.db'],cwd=ROOT,check=True)
    if close_only:
        print('Phase 3 closed: 48 evaluated predictions for each model; database backed up')
        return
    if freeze_only:
        end=min(end,int(datetime.now(timezone.utc).timestamp())//3600*3600)
    # Fixed 14 days ending at the Phase 3 outcome boundary; no Phase 4 outcomes.
    start=end-14*86400
    candles={}
    for chunk in range(start-300,end,86400):
        for candle in fetch_candles(chunk,min(chunk+86400,end)):
            t=int(candle[0])
            if start-300 <= t < end:
                if t in candles and candles[t]!=candle:
                    raise ValueError('Conflicting historical candles')
                candles[t]=candle
    archive=ROOT/'phase4_history_5m.json'
    archive.write_text(json.dumps(sorted(candles.values()),separators=(',',':'))+'\n')
    history=[]; excluded=[]
    for hour in range(start,end,3600):
        try:
            history.append(realized_volatility(list(candles.values()),hour))
        except ValueError:
            excluded.append(hour)
    if len(history)<300:
        raise RuntimeError(f'Insufficient complete historical hours: {len(history)}; need 300')
    config=dict(version=1,phase='phase4',target='next_1h_realized_volatility',
                rv_definition='100 * sqrt(sum(log(C_i / C_(i-1))^2)), 12 consecutive 5m returns',
                quiet_upper=quantile(history,1/3),active_lower=quantile(history,2/3),
                quantiles=[1/3,2/3],quantile_method='linear interpolation (n-1)*q',
                calibration_start=start,calibration_end=end,historical_hours=len(history),
                excluded_hours=excluded,prediction_limit=48,
                tie_order=list(CLASSES),source='Coinbase BTC-USD 5m closes')
    if not 0<config['quiet_upper']<config['active_lower']:
        raise RuntimeError('Degenerate thresholds')
    counts={label:sum(classify(rv,config)==label for rv in history) for label in CLASSES}
    config['historical_class_counts']=counts
    config['majority_class']=max(CLASSES,key=lambda label:counts[label])
    import hashlib
    config['history_sha256']=hashlib.sha256(archive.read_bytes()).hexdigest()
    init_schema()
    with sqlite3.connect(DB_PATH) as c:
        c.execute('INSERT INTO phase4_configs VALUES (?,?)',(config_hash(config),json.dumps(config,sort_keys=True)))
    CONFIG_PATH.write_text(json.dumps(config,indent=2)+'\n')
    print(json.dumps(config,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare',action='store_true')
    mode.add_argument('--freeze-thresholds',action='store_true')
    mode.add_argument('--close-phase3',action='store_true')
    args=parser.parse_args()
    prepare(freeze_only=args.freeze_thresholds,close_only=args.close_phase3)
