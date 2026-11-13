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


VERSION='event-200bars-3frames-v1'
APP_ID=0x50384454
FRAMES=(300,900,3600)


def connect(path, readonly=False):
    c=sqlite3.connect(Path(path).resolve().as_uri()+('?mode=ro' if readonly else '?mode=rwc'),uri=True,timeout=20)
    app=c.execute('PRAGMA application_id').fetchone()[0]
    if not readonly and app==0:
        c.execute('PRAGMA application_id=%d'%APP_ID)
        c.execute('CREATE TABLE candles(symbol TEXT, seconds INTEGER, start INTEGER, raw TEXT, observed REAL, PRIMARY KEY(symbol,seconds,start))')
        c.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        c.commit()
    elif app!=APP_ID:c.close();raise ValueError('not_phase8pre2_technical_archive')
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
                request=Request('https://api.exchange.coinbase.com/products/'+symbol+'/candles?'+query,headers={'User-Agent':'btc-phase8pre2/1'})
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
    if symbol not in SYMBOLS:raise ValueError('unsupported_symbol')
    frames={};archive={}
    with closing(connect(path,True)) as c:
        for seconds in FRAMES:
            latest=c.execute('SELECT max(start) FROM candles WHERE symbol=? AND seconds=? AND observed<=? AND start+seconds+60<=?',(symbol,seconds,cutoff,cutoff)).fetchone()[0]
            if latest is None or cutoff-(latest+seconds)>seconds+180:raise ValueError('technical_stale')
            first=latest-199*seconds
            records=c.execute('SELECT start,raw,observed FROM candles WHERE symbol=? AND seconds=? AND start BETWEEN ? AND ? AND observed<=? ORDER BY start',(symbol,seconds,first,latest,cutoff)).fetchall()
            if [r[0] for r in records]!=list(range(first,latest+1,seconds)):raise ValueError('technical_history_missing_or_late')
            rows=[json.loads(r[1]) for r in records]
            for row,(_,_,observed) in zip(rows,records):
                validate(row,first,latest+seconds,observed)
                if row[0]+seconds+60>observed:raise ValueError('incomplete_candle')
            frames[str(seconds)]=frame_features(rows,seconds)
            archive[str(seconds)]=[dict(id=digest([symbol,seconds,r[0],r[1],r[2]]),raw=row,observed_at=r[2]) for r,row in zip(records,rows)]
    snapshot=dict(version=VERSION,symbol=symbol,cutoff=cutoff,candles=archive)
    state=dict(version=VERSION,source='coinbase-exchange',snapshot_id=digest(snapshot),
        units={'returns_percent':'percent; 1.0 means +1%','price':'USD','volume':'base asset',
               'imbalance':'bid size / (bid + ask size); 0.5 balanced',
               'buy_ratio':'aggressive buy volume / total volume; 0.5 balanced',
               'microprice_offset':'microprice minus midpoint in USD'},
        timeframes=frames,recent_5m_candles=[r['raw'] for r in archive['300'][-12:]],
        candle_columns=['start_utc_epoch','low','high','open','close','volume'])
    return state,snapshot
