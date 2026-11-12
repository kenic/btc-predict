"""First-observed five-minute candles; no backdated availability or model calls."""
import argparse
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from candle_store import validate
from .core import SYMBOLS, canonical, digest

VERSION = '5m-200bars-sma20-50-wilder14-macd12-26-9-v1'
APP_ID = 0x50384254


def connect(path, readonly=False):
    c = sqlite3.connect(Path(path).resolve().as_uri()+('?mode=ro' if readonly else '?mode=rwc'), uri=True, timeout=10)
    try:
        app_id = c.execute('PRAGMA application_id').fetchone()[0]
        if not readonly and app_id == 0:
            c.execute('PRAGMA application_id=%d' % APP_ID)
            c.execute('CREATE TABLE candles(symbol TEXT, start INTEGER, raw TEXT, observed REAL, PRIMARY KEY(symbol,start))')
            c.commit()
        elif app_id != APP_ID:
            raise ValueError('not_technical_archive')
        if readonly:c.execute('PRAGMA query_only=ON')
        return c
    except Exception:
        c.close();raise


def save(path, symbol, rows, observed):
    if symbol not in SYMBOLS:raise ValueError('unsupported_symbol')
    valid=[validate(r,0,observed,observed) for r in rows]
    if any(r[5] is None for r in valid):raise ValueError('volume_required')
    with closing(connect(path)) as c, c:
        for r in valid:
            c.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?,?)',(symbol,r[0],canonical(r),observed))


def collect(path, clock=time.time):
    # Once per hour, before the cutoff. 220 bars cover startup and brief outages.
    # Coinbase may return rows outside the requested interval: explicitly filter.
    end=(int(clock()-60)//300)*300
    start=end-220*300
    iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    results={}
    for symbol in SYMBOLS:
        try:
            query=urlencode(dict(start=iso(start),end=iso(end),granularity=300))
            request=Request('https://api.exchange.coinbase.com/products/'+symbol+'/candles?'+query,
                            headers={'User-Agent':'btc-phase8b-archive/1'})
            with urlopen(request,timeout=15) as response:body=response.read(262145)
            if len(body)>262144:raise ValueError('oversized_response')
            rows=[r for r in json.loads(body) if start<=r[0]<end]
            if not rows:raise ValueError('empty_response')
            save(path,symbol,rows,clock())
            results[symbol]={'status':'ok','received':len(rows)}
        except Exception:
            results[symbol]={'status':'error','error':'public_candle_fetch_failed'}
    return results


def ema(values, period):
    # SMA seed at period-1; all remaining values use alpha=2/(period+1).
    result=[None]*(period-1)+[sum(values[:period])/period]
    for v in values[period:]:result.append(result[-1]+2/(period+1)*(v-result[-1]))
    return result


def features(rows):
    close=[r[4] for r in rows];volume=[r[5] for r in rows]
    delta=[b-a for a,b in zip(close,close[1:])]
    gain=sum(max(d,0) for d in delta[:14])/14
    loss=sum(max(-d,0) for d in delta[:14])/14
    for d in delta[14:]:gain=(13*gain+max(d,0))/14;loss=(13*loss+max(-d,0))/14
    rsi=50 if gain==loss==0 else 100 if loss==0 else 100-100/(1+gain/loss)
    fast,slow=ema(close,12),ema(close,26)
    macd=[a-b for a,b in zip(fast[25:],slow[25:])]
    signal=ema(macd,9)[-1]
    tr=[max(r[2]-r[1],abs(r[2]-prev[4]),abs(r[1]-prev[4])) for prev,r in zip(rows,rows[1:])]
    atr=sum(tr[:14])/14
    for value in tr[14:]:atr=(13*atr+value)/14
    vol=sum(volume[-21:-1])/20
    out=dict(close_usd=close[-1],sma20_usd=sum(close[-20:])/20,sma50_usd=sum(close[-50:])/50,
             rsi14=rsi,macd_usd=macd[-1],macd_signal_usd=signal,atr14_usd=atr,
             return_1h=close[-1]/close[-13]-1,return_4h=close[-1]/close[-49]-1,
             volume_ratio20=volume[-1]/vol if vol else None)
    return {k:round(v,8) if v is not None else None for k,v in out.items()}


def freeze(path, symbol, cutoff):
    if symbol not in SYMBOLS or cutoff%300:raise ValueError('invalid_cutoff_or_symbol')
    # Fixed five-minute lag: latest bar ends at cutoff-300, collected before cutoff.
    last=cutoff-600;first=last-199*300
    with closing(connect(path,True)) as c:
        records=c.execute('SELECT start,raw,observed FROM candles WHERE symbol=? AND start BETWEEN ? AND ? AND observed<=? ORDER BY start',
                          (symbol,first,last,cutoff)).fetchall()
    if [r[0] for r in records]!=list(range(first,last+1,300)):
        raise ValueError('technical_history_missing_or_not_known_at_cutoff')
    rows=[validate(json.loads(raw),ts,ts+300,observed) for ts,raw,observed in records]
    archive=[{'id':digest({'source':'coinbase-exchange:'+symbol+':300','raw':row,'observed_at':r[2]}),
              'raw':row,'observed_at':r[2]} for row,r in zip(rows,records)]
    snapshot={'symbol':symbol,'cutoff':cutoff,'version':VERSION,'candles':archive}
    state={'version':VERSION,'source':'coinbase-exchange','bar_seconds':300,'bars':200,
           'latest_close_at':last+300,'snapshot_id':digest(snapshot),**features(rows)}
    return state,snapshot


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--db',required=True);args=p.parse_args()
    result=collect(args.db);print(canonical(result))
    raise SystemExit(0 if all(r['status']=='ok' for r in result.values()) else 1)
