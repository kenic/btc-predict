"""Public market archive with bounded BTC gap repair and immutable availability."""
import argparse
import hashlib
import json
import math
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from .core import SYMBOLS,canonical,digest,timestamp

APP_ID=0x5038434d
WINDOWS=(1,5,15,60)
REPAIR_LOOKBACK_SECONDS=3900
REPAIR_MAX_PAGES=2
REPAIR_VERSION='btc-recent-id-gap-repair-v1'
BOOK_KEYS=('spread','top5_imbalance','top10_imbalance','top20_imbalance','microprice_offset')


def connect(path,readonly=False):
    c=sqlite3.connect(Path(path).resolve().as_uri()+('?mode=ro' if readonly else '?mode=rwc'),uri=True,timeout=20)
    app=c.execute('PRAGMA application_id').fetchone()[0]
    if app==0 and not readonly:
        c.execute('PRAGMA application_id=%d'%APP_ID)
        c.execute('CREATE TABLE books(symbol TEXT,observed REAL,raw TEXT,PRIMARY KEY(symbol,observed))')
        c.execute('CREATE TABLE trades(symbol TEXT,id INTEGER,event REAL,observed REAL,raw TEXT,PRIMARY KEY(symbol,id))')
        c.execute('CREATE INDEX trade_event ON trades(symbol,event)')
        c.execute('CREATE TABLE polls(symbol TEXT,observed REAL,complete INTEGER,PRIMARY KEY(symbol,observed))');c.commit()
    elif app!=APP_ID:c.close();raise ValueError('not_phase8c_micro_archive')
    if not readonly:
        c.execute('CREATE TABLE IF NOT EXISTS trade_repairs(symbol TEXT, observed REAL, anchor INTEGER, status TEXT, fetched INTEGER, inserted INTEGER, response_hash TEXT, PRIMARY KEY(symbol,observed,anchor))')
        c.commit()
    if readonly:c.execute('PRAGMA query_only=ON')
    return c


def get(symbol,resource,**query):
    request=Request('https://api.exchange.coinbase.com/products/'+symbol+'/'+resource+'?'+urlencode(query),headers={'User-Agent':'btc-phase8c/1'})
    with urlopen(request,timeout=8) as response:
        data=response.read(4*1024*1024+1);cursor=response.headers.get('CB-AFTER')
    if len(data)>4*1024*1024:raise ValueError('oversized_response')
    return json.loads(data),cursor


def book_features(book):
    sides=[]
    for side in ('bids','asks'):
        values=[[float(v[0]),float(v[1])] for v in book[side][:20]]
        if len(values)<20 or any(not math.isfinite(v) or v<=0 for row in values for v in row):raise ValueError('invalid_book')
        sides.append(values)
    bids,asks=sides;b,bs=bids[0];a,asz=asks[0]
    if a<b:raise ValueError('crossed_book')
    out=dict(best_bid=b,best_ask=a,spread=a-b,microprice_offset=(a*bs+b*asz)/(bs+asz)-(a+b)/2)
    for n in (5,10,20):
        bv=sum(x[1] for x in bids[:n]);av=sum(x[1] for x in asks[:n]);out['top'+str(n)+'_imbalance']=bv/(bv+av)
    return out


def trade(row):
    event=timestamp(row['time']);price=float(row['price']);size=float(row['size']);tid=int(row['trade_id'])
    if row['side'] not in ('buy','sell') or not math.isfinite(price+size) or price<=0 or size<=0 or tid<0:raise ValueError('invalid_trade')
    return dict(id=tid,event=event,price=price,size=size,aggressor='buy' if row['side']=='sell' else 'sell')


def recent_gaps(path,symbol,now):
    """Audit IDs before time filtering; include the preceding archived boundary ID.

    Only already observed rows may steer repair. A 65m window covers the next
    hourly 60m input, including its left boundary. No missing-ID expansion.
    """
    with closing(connect(path,True)) as c:
        first=c.execute('SELECT min(id) FROM trades WHERE symbol=? AND event>=? AND event<=? AND observed<=?',
                        (symbol,now-REPAIR_LOOKBACK_SECONDS,now,now)).fetchone()[0]
        if first is None:return []
        previous=c.execute('SELECT max(id) FROM trades WHERE symbol=? AND id<? AND observed<=?',(symbol,first,now)).fetchone()[0]
        ids=[r[0] for r in c.execute('SELECT id FROM trades WHERE symbol=? AND id>=? AND observed<=? ORDER BY id',
                                   (symbol,previous if previous is not None else first,now))]
    return [(a+1,b-1,b) for a,b in zip(ids,ids[1:]) if b>a+1]


def repair_recent(path,symbol,clock=time.time):
    """Public GETs only; never called by prediction/freeze or outcome evaluation.

    Request older trades at the known right-hand cursor adjacent to a gap.
    Two pages maximum per poll; unresolved gaps retry during later collection,
    not during prediction. Responses are newly available at actual receipt time.
    """
    if symbol!='BTC-USD':return dict(pages=0,inserted=0)
    tried=set();total=0;pages=0
    for _ in range(REPAIR_MAX_PAGES):
        gap=next((g for g in recent_gaps(path,symbol,clock()) if g[2] not in tried),None)
        if gap is None:break
        anchor=gap[2];tried.add(anchor);pages+=1
        status='failed';parsed=[];inserted=0;response_hash=None
        try:
            page,_=get(symbol,'trades',after=str(anchor),limit=1000)
            parsed=[trade(r) for r in page];observed=clock()
            if not parsed or len(parsed)>1000 or len({r['id'] for r in parsed})!=len(parsed):raise ValueError('invalid_repair_page')
            if any(r['event']>observed or r['id']>=anchor for r in parsed):raise ValueError('invalid_repair_range')
            response_hash=digest(parsed)
            with closing(connect(path)) as c,c:
                for r in parsed:
                    cur=c.execute('INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?)',(symbol,r['id'],r['event'],observed,canonical(r)))
                    inserted+=cur.rowcount
            total+=inserted;status='stored'
        except Exception:
            observed=clock()
        with closing(connect(path)) as c,c:
            c.execute('INSERT OR IGNORE INTO trade_repairs VALUES(?,?,?,?,?,?,?)',
                      (symbol,observed,anchor,status,len(parsed),inserted,response_hash))
    return dict(pages=pages,inserted=total)


def collect_symbol(path,symbol,clock=time.time):
    try:
        if symbol!='BTC-USD':
            book,_=get(symbol,'book',level=2);observed=clock();features=book_features(book)
            with closing(connect(path)) as c,c:c.execute('INSERT INTO books VALUES(?,?,?)',(symbol,observed,canonical(features)))
        with closing(connect(path,True)) as c:previous=c.execute('SELECT max(id) FROM trades WHERE symbol=?',(symbol,)).fetchone()[0]
        rows=[];cursor=None;complete=False
        for _ in range(10):
            query={'limit':1000}
            if cursor:query['after']=cursor
            page,cursor=get(symbol,'trades',**query);parsed=[trade(r) for r in page]
            if not parsed:raise ValueError('empty_trades')
            rows.extend(parsed)
            if previous is None or min(r['id'] for r in parsed)<=previous:
                complete=previous is not None;break
            if not cursor:break
        observed=clock()
        if any(r['event']>observed for r in rows):raise ValueError('future_trade')
        with closing(connect(path)) as c,c:
            for r in rows:c.execute('INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?)',(symbol,r['id'],r['event'],observed,canonical(r)))
            c.execute('INSERT INTO polls VALUES(?,?,?)',(symbol,observed,int(complete)))
        repair=repair_recent(path,symbol,clock)
        return {'symbol':symbol,'status':'ok','continuous':complete,'gap_repair':repair}
    except Exception:return {'symbol':symbol,'status':'public_micro_fetch_failed'}


def coverage(times,cutoff):
    # At most 45s between observations, and latest <=30s old. No invented warm-up.
    if not times or times[0]>cutoff-3600 or times[-1]<cutoff-30 or any(b-a>45 for a,b in zip(times,times[1:])):
        raise ValueError('micro_coverage_incomplete')


def aggregate(books,trades,cutoff):
    result={'current':books[-1][1],'latest_observed_at':books[-1][0],'orderbook_averages':{},'trade_flow':{}}
    for minutes in WINDOWS:
        bs=[v for t,v in books if t>cutoff-minutes*60]
        ts=[v for v in trades if v['event']>cutoff-minutes*60]
        if not bs:raise ValueError('missing_book_window')
        result['orderbook_averages'][str(minutes)]=dict(samples=len(bs),**{k:sum(b[k] for b in bs)/len(bs) for k in BOOK_KEYS})
        buy=sum(v['size'] for v in ts if v['aggressor']=='buy');sell=sum(v['size'] for v in ts if v['aggressor']=='sell')
        result['trade_flow'][str(minutes)]=dict(trades=len(ts),buy_volume=buy,sell_volume=sell,buy_ratio=buy/(buy+sell) if buy+sell else None)
    # Short decimal representations reduce request size, not probability outputs.
    def round_values(v):
        if isinstance(v,dict):return {k:round_values(x) for k,x in v.items()}
        return round(v,8) if isinstance(v,float) else v
    return round_values(result)


def freeze(path,symbol,cutoff,btc_market_db=None):
    if symbol=='BTC-USD' and btc_market_db:
        with closing(sqlite3.connect(Path(btc_market_db).resolve().as_uri()+'?mode=ro',uri=True,timeout=15)) as c:
            c.row_factory=sqlite3.Row
            rawbooks=[dict(r) for r in c.execute('SELECT * FROM orderbook_samples WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp',(cutoff-3645,cutoff))]
            rawbooks=[r for r in rawbooks if timestamp(r['created_at'])<=cutoff]
            books=[(timestamp(r['created_at']),{k:r[k] for k in ('best_bid','best_ask')+BOOK_KEYS}) for r in rawbooks if timestamp(r['created_at'])<=cutoff]
            books.sort(key=lambda r:r[0]);coverage([t for t,_ in books],cutoff)
            rawtrades=[dict(r) for r in c.execute('SELECT trade_id,timestamp,size,aggressor_side,collected_at FROM trades WHERE timestamp>? AND timestamp<=? ORDER BY timestamp,trade_id',(cutoff-3600,cutoff))]
            rawtrades=[r for r in rawtrades if timestamp(r['collected_at'])<=cutoff]
        # Supplement the legacy 1000-trade polling with bounded, audited pagination.
        if Path(path).exists():
            with closing(connect(path,True)) as c:
                extra=c.execute('SELECT id,event,observed,raw FROM trades WHERE symbol=? AND event>? AND event<=? AND observed<=? ORDER BY event,id',(symbol,cutoff-3600,cutoff,cutoff)).fetchall()
            merged={r['trade_id']:r for r in rawtrades}
            for tid,event,observed,raw in extra:
                v=json.loads(raw)
                if tid in merged and (merged[tid]['size']!=v['size'] or merged[tid]['aggressor_side']!=v['aggressor']):raise ValueError('trade_source_conflict')
                merged.setdefault(tid,dict(trade_id=tid,timestamp=event,size=v['size'],aggressor_side=v['aggressor'],collected_at=observed))
            rawtrades=sorted(merged.values(),key=lambda v:(v['timestamp'],v['trade_id']))
        trades=[dict(event=r['timestamp'],size=r['size'],aggressor=r['aggressor_side']) for r in rawtrades]
        # Detect missing trades despite the legacy collector's non-paginated polling.
        if not trades or max(r['event'] for r in trades)<cutoff-30:raise ValueError('stale_btc_trades')
        ids=[r['trade_id'] for r in rawtrades]
        if len(ids)!=max(ids)-min(ids)+1 or min(r['event'] for r in trades)>cutoff-3570:
            raise ValueError('btc_trade_coverage_gap')
        provenance=dict(source='existing-btc-market.db+phase8c-micro.sqlite3',cutoff=cutoff,books_hash=digest(rawbooks),trades_hash=digest(rawtrades),
            trade_count=len(trades),first_trade_id=rawtrades[0]['trade_id'],last_trade_id=rawtrades[-1]['trade_id'],
            availability_filter='created_at/collected_at <= cutoff',trade_coverage='contiguous trade IDs; first and latest trade within 30s of window boundaries')
    else:
        with closing(connect(path,True)) as c:
            br=c.execute('SELECT observed,raw FROM books WHERE symbol=? AND observed BETWEEN ? AND ? ORDER BY observed',(symbol,cutoff-3645,cutoff)).fetchall()
            polls=c.execute('SELECT observed,complete FROM polls WHERE symbol=? AND observed BETWEEN ? AND ? ORDER BY observed',(symbol,cutoff-3645,cutoff)).fetchall()
            coverage([r[0] for r in br],cutoff);coverage([r[0] for r in polls],cutoff)
            if any(not complete for observed,complete in polls if observed>cutoff-3600):raise ValueError('trade_pagination_gap')
            tr=c.execute('SELECT id,event,observed,raw FROM trades WHERE symbol=? AND event>? AND event<=? AND observed<=? ORDER BY event,id',(symbol,cutoff-3600,cutoff,cutoff)).fetchall()
        books=[(t,json.loads(raw)) for t,raw in br];trades=[json.loads(r[3]) for r in tr]
        provenance=dict(source='phase8c-micro.sqlite3',symbol=symbol,cutoff=cutoff,books=br,polls=polls,
            trades_hash=digest(tr),trade_count=len(tr),trade_ids=[r[0] for r in tr])
    return aggregate(books,trades,cutoff),provenance


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--once',action='store_true');a=p.parse_args()
    with closing(connect(a.db)):pass
    while True:
        started=time.monotonic()
        with ThreadPoolExecutor(max_workers=5) as pool:result=list(pool.map(lambda s:collect_symbol(a.db,s),SYMBOLS))
        print(canonical(result),flush=True)
        if a.once:break
        time.sleep(max(1,15-(time.monotonic()-started)))
