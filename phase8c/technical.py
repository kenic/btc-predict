"""Phase-6 feature family, with immutable first-observed candles and explicit units."""
import argparse
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from candle_store import validate
from .core import SYMBOLS, canonical, digest
from .indicators import calculate_timeframe_indicators, pct_return
from . import micro

VERSION='phase6-family-200bars-3frames-micro-v1'
APP_ID=0x50384354
FRAMES=(300,900,3600)


def connect(path, readonly=False):
    c=sqlite3.connect(Path(path).resolve().as_uri()+('?mode=ro' if readonly else '?mode=rwc'),uri=True,timeout=20)
    app=c.execute('PRAGMA application_id').fetchone()[0]
    if not readonly and app==0:
        c.execute('PRAGMA application_id=%d'%APP_ID)
        c.execute('CREATE TABLE candles(symbol TEXT, seconds INTEGER, start INTEGER, raw TEXT, observed REAL, PRIMARY KEY(symbol,seconds,start))')
        c.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        c.commit()
    elif app!=APP_ID:c.close();raise ValueError('not_phase8c_technical_archive')
    if readonly:c.execute('PRAGMA query_only=ON')
    return c


def save(path,symbol,seconds,rows,observed):
    if symbol not in SYMBOLS or seconds not in FRAMES:raise ValueError('unsupported_series')
    valid=[]
    for row in rows:
        r=validate(row,0,observed,observed)
        if r[0]%seconds or r[0]+seconds+60>observed or r[5] is None:raise ValueError('incomplete_candle')
        valid.append(r)
    with closing(connect(path)) as c,c:
        for r in valid:c.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?)',(symbol,seconds,r[0],canonical(r),observed))


def collect(path,clock=time.time):
    result={}
    for symbol in SYMBOLS:
        for seconds in FRAMES:
            try:
                end=int(clock()-60)//seconds*seconds;start=end-205*seconds
                iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
                query=urlencode(dict(start=iso(start),end=iso(end),granularity=seconds))
                request=Request('https://api.exchange.coinbase.com/products/'+symbol+'/candles?'+query,headers={'User-Agent':'btc-phase8c/1'})
                with urlopen(request,timeout=10) as response:body=response.read(262145)
                if len(body)>262144:raise ValueError('oversized_response')
                rows=[r for r in json.loads(body) if start<=r[0]<end]
                if not rows:raise ValueError('empty_response')
                save(path,symbol,seconds,rows,clock());result[symbol+':'+str(seconds)]='ok'
            except Exception:result[symbol+':'+str(seconds)]='public_candle_fetch_failed'
    return result


def frame_features(rows,seconds):
    df=calculate_timeframe_indicators(rows);x=df.iloc[-1]
    # Phase 6's pandas EWM, sample Bollinger std and current-inclusive volume mean.
    names=('sma20','sma50','rsi14','macd','macd_signal','macd_hist','bb_position','volume_ratio')
    out={k:(round(float(x[k]),8) if math.isfinite(float(x[k])) else None) for k in names}
    periods={300:(1,3,6,12),900:(1,2,4,16),3600:(1,4,24)}[seconds]
    out['returns_percent']={str(p*seconds):round(float(pct_return(df,p)),8) for p in periods}
    return dict(bar_seconds=seconds,bars=len(rows),latest_close_at=rows[-1][0]+seconds,close_usd=rows[-1][4],**out)


def freeze(path,symbol,cutoff):
    if symbol not in SYMBOLS or cutoff%3600:raise ValueError('hourly_cutoff_required')
    frames={};archive={}
    with closing(connect(path,True)) as c:
        for seconds in FRAMES:
            # Last bar ends one interval BEFORE cutoff: known before decision time.
            last=cutoff-2*seconds;first=last-199*seconds
            records=c.execute('SELECT start,raw,observed FROM candles WHERE symbol=? AND seconds=? AND start BETWEEN ? AND ? AND observed<=? ORDER BY start',(symbol,seconds,first,last,cutoff)).fetchall()
            if [r[0] for r in records]!=list(range(first,last+1,seconds)):raise ValueError('technical_history_missing_or_late')
            rows=[json.loads(r[1]) for r in records]
            for row,(_,_,observed) in zip(rows,records):
                validate(row,first,last+seconds,observed)
                if row[0]+seconds+60>observed:raise ValueError('incomplete_candle')
            frames[str(seconds)]=frame_features(rows,seconds)
            archive[str(seconds)]=[dict(id=digest([symbol,seconds,r[0],r[1],r[2]]),raw=row,observed_at=r[2]) for r,row in zip(records,rows)]
        setting=c.execute('SELECT value FROM settings WHERE key="btc_market_db"').fetchone()
    structure,provenance=micro.freeze(Path(path).with_name('micro.sqlite3'),symbol,cutoff,setting[0] if setting else None)
    snapshot=dict(version=VERSION,symbol=symbol,cutoff=cutoff,candles=archive,microstructure=provenance)
    state=dict(version=VERSION,source='coinbase-exchange',snapshot_id=digest(snapshot),
        units={'returns_percent':'percent; 1.0 means +1%','price':'USD','volume':'base asset',
               'imbalance':'bid size / (bid + ask size); 0.5 balanced',
               'buy_ratio':'aggressive buy volume / total volume; 0.5 balanced',
               'microprice_offset':'microprice minus midpoint in USD'},
        timeframes=frames,recent_5m_candles=[r['raw'] for r in archive['300'][-12:]],
        candle_columns=['start_utc_epoch','low','high','open','close','volume'],microstructure=structure)
    return state,snapshot


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--btc-market-db');args=p.parse_args()
    with closing(connect(args.db)) as c,c:
        if args.btc_market_db:c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('btc_market_db',args.btc_market_db))
    result=collect(args.db);print(canonical(result));raise SystemExit(0 if all(v=='ok' for v in result.values()) else 1)
