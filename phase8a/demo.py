"""A fully local synthetic archive -> four fixture responses -> price matching demo."""
import json
from pathlib import Path
from news import store as news_store
from news.feeds import load_config, parse_rss
from .runtime import run
from .evaluation import evaluate, report
from .core import canonical
import candle_store

ROOT = Path(__file__).resolve().parent.parent


def build_demo(directory):
    path = Path(directory)
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    t = 1794398400 // 14400 * 14400
    config = json.loads((ROOT/'phase8a.config.example.json').read_text())
    news = path/'news.db'
    db = path/'phase8a.db'
    candles = path/'candles.db'
    news_store.initialize(news)
    source = load_config(ROOT/'news.config.example.json')['feeds'][1]
    from datetime import datetime, timezone
    pub = datetime.fromtimestamp(t-120, timezone.utc).isoformat()
    body = ('<rss><channel><item><guid>fixture</guid><title>SYNTHETIC inflation announcement</title>'
            '<link>https://example.org/synthetic</link><pubDate>'+pub+'</pubDate>'
            '<description>Fixture only, not a real announcement.</description></item></channel></rss>').encode()
    entries, rejected = parse_rss(body, source)
    rid = news_store.start_run(news, source, 3600, lambda: t-100)
    news_store.finish_run(news, rid, 'ok', entries=entries, rejected=rejected, clock=lambda: t-90)
    run(db, news, config, t, clock=lambda: t+10)
    candle_store.initialize(candles, t-600)
    for ts, price in ((t-300, 100), (t+300, 101), (t+14400-300, 102)):
        candle_store.save(candles, [[ts, price-1, price+1, price, price, 10]], ts+360)
    evaluate(db, candles, lambda: t+14400+60)
    output = report(db)
    (path/'report.json').write_text(canonical(output)+'\n')
    return output
