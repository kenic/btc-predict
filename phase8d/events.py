"""First-observed article events; a revision/restart never creates another paid call."""
import json,time
from collections import defaultdict
from news.store import snapshot,connect as news_connect
from . import store
from phase8pre2 import technical,market
from pathlib import Path
from .core import canonical,digest,relevance,SYMBOLS,freeze

def keys(n):
    return ['url:'+n['url'],'text:'+digest(' '.join((n['title']+' '+n['summary']).lower().split()))]

def bootstrap(db,news_db):
    with news_connect(news_db,True) as n:maximum=n.execute('SELECT coalesce(max(id),0) FROM versions').fetchone()[0]
    with store.connect(db) as c,c:
        if c.execute("SELECT 1 FROM settings WHERE key='cursor'").fetchone():return
        c.execute('INSERT INTO settings VALUES(?,?)',('cursor',str(maximum)))
        started=float(c.execute("SELECT value FROM settings WHERE key='started'").fetchone()[0])
        for item in snapshot(news_db,started)['news']:
            for symbol in SYMBOLS:
                for key in keys(item):c.execute('INSERT OR IGNORE INTO seen VALUES(?,?)',(symbol,key))

def discover(db,news_db,clock=time.time):
    """Queue at each committed feed receipt time; never wait for another source."""
    with store.connect(db,True) as c:
        cursor=int(c.execute("SELECT value FROM settings WHERE key='cursor'").fetchone()[0])
        started=float(c.execute("SELECT value FROM settings WHERE key='started'").fetchone()[0])
    with news_connect(news_db,True) as n:
        n.execute('BEGIN')
        rows=n.execute('''SELECT v.id,v.article_id,v.available_at,v.run_id FROM versions v
          WHERE v.id>? AND v.id=(SELECT min(w.id) FROM versions w WHERE w.article_id=v.article_id)
          ORDER BY v.id''',(cursor,)).fetchall()
        maximum=n.execute('SELECT coalesce(max(id),0) FROM versions').fetchone()[0]
    groups=defaultdict(list)
    for r in rows:
        if r['available_at']>started:groups[(r['run_id'],r['available_at'])].append(r['article_id'])
    with store.connect(db) as c,c:
        for (run,T),ids in groups.items():
            snap=snapshot(news_db,T)
            healthy={x['feed_id'] for x in snap['coverage'] if x['enabled'] and not x['stale']}
            for symbol in SYMBOLS:
                items=[]
                for item in snap['news']:
                    if item['article_id'] not in ids or item['source_id'] not in healthy or item['language']!='en' or not relevance(item,symbol):continue
                    kk=keys(item)
                    duplicate=any(c.execute('SELECT 1 FROM seen WHERE symbol=? AND key=?',(symbol,k)).fetchone() for k in kk)
                    for key in kk:c.execute('INSERT OR IGNORE INTO seen VALUES(?,?)',(symbol,key))
                    if not duplicate:items.append(item)
                if items:
                    eid=digest(['phase8d',symbol,run,T])
                    c.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',(eid,symbol,T,T+14400,'live','queued',None,canonical(items),None,clock(),None))
        c.execute("UPDATE settings SET value=? WHERE key='cursor'",(str(maximum),))

def prepare(db,news_db,technical_db,event,config,quotes_db=None):
    T=event['cutoff'];symbol=event['symbol'];triggers=json.loads(event['trigger_json'])
    snap=snapshot(news_db,T);f=freeze(snap,config,T)
    eligible={n['article_id']:n for n in snap['news']}
    # New items lead. Bounded background follows; every trigger version stays in provenance.
    picked=[];seen=set();trigger_ids={n['article_id'] for n in triggers}
    for n in triggers+f['state']['news']:
        if n['article_id'] in seen:continue
        current=eligible.get(n['article_id'])
        if current is None or current['version_id']!=n['version_id'] or current['source_id'] not in config['allowed_sources']:raise ValueError('trigger_not_available')
        item={k:current[k] for k in ('article_id','version_id','source_id','published_at','available_at','title','url','attribution')}
        item.update(summary=current['summary'][:700],relevance=relevance(current,symbol),is_trigger=n['article_id'] in trigger_ids)
        if len(picked)>=config['max_news'] or len(canonical(picked+[item]).encode())>config['max_input_bytes']:continue
        picked.append(item);seen.add(n['article_id'])
    if not picked or not any(n['is_trigger'] for n in picked):raise ValueError('trigger_input_too_large')
    tech,archive=technical.freeze(technical_db,symbol,T)
    with market.connect(quotes_db or Path(technical_db).with_name('quotes.sqlite3'),True) as c:q=market.before(c,symbol,T)
    if q is None:raise ValueError('reference_quote_missing')
    ns={'symbol':symbol,'cutoff':T,'news':picked,'triggers':[(n['article_id'],n['version_id']) for n in triggers]}
    f['state'].update(news=picked,news_set_id=digest(ns),technical=tech,reference_price=market.midpoint(q),
       trigger_article_versions=ns['triggers'],reference_price_observed_at=q['observed'])
    f.update(news_set=ns,technical_snapshot=archive,input_id=digest(f['state']),reference_quote=q)
    return f
