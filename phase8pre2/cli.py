"""Independent technical and quote archive. Only unauthenticated market GETs."""
import argparse,time,json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from . import technical,market
from .core import SYMBOLS

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=('quotes','technical','once'));p.add_argument('--directory',required=True);a=p.parse_args()
    root=Path(a.directory);root.mkdir(parents=True,exist_ok=True);quotes=root/'quotes.sqlite3';market.initialize(quotes)
    while True:
        statuses=[]
        if a.command=='quotes':
            def one(symbol):
                try:market.collect(quotes,symbol);return 'ok'
                except Exception:return 'quote_fetch_failed'
            with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(one,SYMBOLS))
            market.heartbeat(quotes,'quotes','ok' if all(s=='ok' for s in statuses) else 'degraded',time.time())
            time.sleep(2)
        else:
            try:
                result=technical.collect(root/'technical.sqlite3');status='ok' if all(v=='ok' for v in result.values()) else 'degraded'
            except Exception:status='technical_fetch_failed'
            market.heartbeat(quotes,'technical',status,time.time())
            print(json.dumps({'worker':'technical','status':status}),flush=True)
            if a.command=='once':return
            time.sleep(30)

if __name__=='__main__':main()
