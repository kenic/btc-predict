"""Public, read-only Phase 8pre view. No collector/model imports or external I/O."""
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from ui import card, metric_blocks, page_shell

SOURCES = {
    'frb_press': 'Federal Reserve Board',
    'bls_cpi': 'BLS · Consumer prices',
    'bls_empsit': 'BLS · Employment',
    'bls_jolts': 'BLS · Job openings',
    'coindesk': 'CoinDesk',
}
# Public projection: reviewed government feeds plus CoinDesk RSS, explicitly
# approved by the operator on 2026-11-12. Never expose raw RSS or private config.
PUBLIC_HOSTS = {
    'frb_press': 'federalreserve.gov',
    'bls_cpi': 'bls.gov', 'bls_empsit': 'bls.gov', 'bls_jolts': 'bls.gov',
    'coindesk': 'coindesk.com',
}
WINDOW = 86400
LIMIT = 100
APP_ID = 0x4E455753


def parse_cutoff(value, now):
    if not value:
        return now
    try:
        if len(value) > 64:
            raise ValueError()
        try:
            result = float(value)
        except ValueError:
            d = datetime.fromisoformat(value.replace('Z', '+00:00'))
            # The datetime-local form explicitly labels its timezone as UTC.
            result = d.replace(tzinfo=timezone.utc).timestamp() if d.tzinfo is None else d.timestamp()
        if not math.isfinite(result) or result < 0 or result > now:
            raise ValueError()
        datetime.fromtimestamp(result, timezone.utc)
        return result
    except (ValueError, TypeError, OverflowError, OSError):
        raise ValueError('Choose a valid past date and time (UTC).') from None


def stamp(value):
    return '—' if value is None else datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def public_url(value, source):
    try:
        p = urlsplit(value)
        host = PUBLIC_HOSTS.get(source)
        if (not host or p.scheme != 'https' or p.username or p.password
                or (p.hostname != host and not (p.hostname or '').endswith('.'+host))):
            return None
        return value
    except (ValueError, TypeError):
        return None


def _polling(c, cutoff):
    output = []
    start = cutoff-WINDOW
    for feed in c.execute('SELECT id,first_configured_at FROM feeds WHERE first_configured_at<=? ORDER BY id', (cutoff,)):
        feed_id = feed['id']
        last = c.execute('''SELECT status,available_at,source_json,interval_seconds,rejected,http_status
            FROM runs WHERE feed_id=? AND available_at<=? ORDER BY available_at DESC,id DESC LIMIT 1''',
            (feed_id, cutoff)).fetchone()
        configured = last or c.execute('''SELECT source_json,interval_seconds FROM runs
            WHERE feed_id=? AND started_at<=? ORDER BY started_at,id LIMIT 1''', (feed_id,cutoff)).fetchone()
        if configured is None:
            continue
        source = json.loads(configured['source_json'])
        enabled = source.get('enabled') is True and source.get('policy', {}).get('mode') != 'blocked'
        interval = max(600, min(900, configured['interval_seconds']))
        success = c.execute('''SELECT max(available_at) FROM runs WHERE feed_id=? AND available_at<=?
            AND status IN ('ok','not_modified')''', (feed_id,cutoff)).fetchone()[0]
        rows = c.execute('''SELECT available_at,status,rejected,
            CASE WHEN json_extract(source_json,'$.enabled')=1
              AND json_extract(source_json,'$.policy.mode')!='blocked' THEN 1 ELSE 0 END AS enabled
            FROM runs WHERE feed_id=?
            AND available_at>? AND available_at<=? ORDER BY available_at,id''', (feed_id,start,cutoff)).fetchall()
        successes = [r['available_at'] for r in rows if r['status'] in ('ok','not_modified')]
        # The left edge can start inside an outage, so include the preceding success.
        previous = c.execute('''SELECT max(available_at) FROM runs WHERE feed_id=? AND available_at<=?
            AND status IN ('ok','not_modified')''', (feed_id,start)).fetchone()[0]
        baseline = c.execute('''SELECT source_json FROM runs WHERE feed_id=? AND available_at<=?
            ORDER BY available_at DESC,id DESC LIMIT 1''',(feed_id,start)).fetchone()
        initial = baseline or c.execute('''SELECT source_json FROM runs WHERE feed_id=? AND started_at<=?
            ORDER BY started_at,id LIMIT 1''',(feed_id,cutoff)).fetchone()
        initial_config=json.loads(initial['source_json'])
        initial_enabled=initial_config.get('enabled') is True and initial_config.get('policy',{}).get('mode')!='blocked'
        transitions=[(max(start,feed['first_configured_at']),initial_enabled)]
        for row in rows:
            if bool(row['enabled'])!=transitions[-1][1]:
                transitions.append((row['available_at'],bool(row['enabled'])))
        gaps=[]
        for index,(left,on) in enumerate(transitions):
            right=transitions[index+1][0] if index+1<len(transitions) else cutoff
            if not on:
                continue
            anchor=previous if index==0 and previous is not None else left
            points=[anchor]+[t for t in successes if left<=t<=right]+[right]
            gaps.extend(dict(start=max(a,start),end=b) for a,b in zip(points,points[1:])
                        if b-a>1.5*interval and b>start)
        # 96 complete 15-minute bins ending at the chosen cutoff; unobserved bins
        # are observational coverage, never a fabricated count of missing stories.
        bins = []
        for i in range(96):
            left, right = start+i*900, start+(i+1)*900
            on=next((on for at,on in reversed(transitions) if at<=right),False)
            state = ('before' if right<=feed['first_configured_at'] else 'paused' if not on
                     else 'observed' if any(left<t<=right for t in successes) else 'unobserved')
            bins.append(dict(start=left, end=right, state=state))
        stale = enabled and (success is None or cutoff-success>1.5*interval)
        status = ('paused' if not enabled else 'attention' if stale or (last and
                  (last['status'] in ('failed','deferred') or last['rejected'])) else 'healthy')
        count = c.execute('SELECT count(*) FROM articles WHERE feed_id=? AND first_seen_at<=?',
                          (feed_id,cutoff)).fetchone()[0]
        label=SOURCES.get(feed_id,str(source.get('publisher',feed_id))[:80])
        output.append(dict(id=feed_id, label=label, enabled=enabled, status=status,
            last_success_at=success, latest_result=last['status'] if last else 'running',
            articles=count, successful_polls=len(successes), failed_polls=sum(r['status']=='failed' for r in rows),
            rejected=sum(r['rejected'] for r in rows), gaps=gaps, bins=bins,
            interval_seconds=interval))
    return output


def _articles(c, cutoff, source_filter):
    # Same latest-known-version-before-publication-filter rule as the archive.
    # SQL JSON extraction avoids loading raw response bodies into the web process.
    rows = c.execute('''WITH latest AS (
        SELECT v.id,v.article_id,v.available_at,v.item_json,v.run_id,a.feed_id,a.first_seen_at,
            row_number() OVER (PARTITION BY v.article_id ORDER BY v.available_at DESC,v.id DESC) AS n
        FROM versions v JOIN articles a ON a.id=v.article_id WHERE v.available_at<=?
    ) SELECT v.id,v.article_id,v.available_at,v.item_json,v.feed_id,v.first_seen_at,
        (SELECT max(observed_at) FROM observations WHERE article_id=v.article_id AND observed_at<=?) AS last_seen,
        (SELECT count(*) FROM versions WHERE article_id=v.article_id AND available_at<=?) AS version_count
        FROM latest v JOIN runs r ON r.id=v.run_id
        WHERE v.n=1 AND v.feed_id IN ('frb_press','bls_cpi','bls_empsit','bls_jolts','coindesk')
        AND (?='all' OR v.feed_id=?)
        AND json_extract(r.source_json,'$.policy.model_input')=1
        AND json_type(v.item_json,'$.published_at') IN ('integer','real')
        AND json_extract(v.item_json,'$.published_at')<=?
        ORDER BY json_extract(v.item_json,'$.published_at') DESC,v.feed_id,v.article_id
        LIMIT 1001''', (cutoff,cutoff,cutoff,source_filter,source_filter,cutoff)).fetchall()
    recent, selected, seen = [], [], set()
    for row in rows:
        item = json.loads(row['item_json'])
        url = public_url(item.get('url'),row['feed_id'])
        if not url or url in seen:
            continue
        seen.add(url)
        news = dict(article_id=row['article_id'],version_id=row['id'],source_id=row['feed_id'],
            title=str(item.get('title',''))[:1000],summary=str(item.get('summary',''))[:2000],url=url,
            attribution=str(item.get('attribution',SOURCES[row['feed_id']]))[:300],
            published_at=item['published_at'],available_at=row['available_at'],
            first_fetched_at=row['first_seen_at'],last_fetched_at=row['last_seen'],versions=row['version_count'])
        if len(recent)<6:
            recent.append(news)
        if item['published_at']>cutoff-WINDOW:
            selected.append(news)
    capped = len(rows)==1001 and json.loads(rows[-1]['item_json'])['published_at']>cutoff-WINDOW
    return selected, recent, capped


def report(path, cutoff, source='all'):
    """One read transaction, allowlisted public data, bounded SQL execution time."""
    if source not in ('all', *PUBLIC_HOSTS):
        raise ValueError('Choose one of the listed news sources.')
    result = dict(status='unavailable',as_of=cutoff,window_start=cutoff-WINDOW,source=source,
                  sources=[],news=[],recent=[],eligible_count=0,display_limit=LIMIT,truncated=False,
                  archived_articles=0,archived_versions=0,collection_started_at=None)
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)) as c:
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA query_only=ON')
            deadline=time.monotonic()+2
            c.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
            if c.execute('PRAGMA application_id').fetchone()[0]!=APP_ID or c.execute('PRAGMA user_version').fetchone()[0]!=1:
                return result
            c.execute('BEGIN')
            result['sources']=_polling(c,cutoff)
            selected,recent,capped=_articles(c,cutoff,source)
            result.update(status='ok',news=selected[:LIMIT],recent=recent,
                          eligible_count=len(selected),truncated=capped or len(selected)>LIMIT)
            result['archived_articles']=c.execute('SELECT count(*) FROM articles WHERE first_seen_at<=?',(cutoff,)).fetchone()[0]
            result['archived_versions']=c.execute('SELECT count(*) FROM versions WHERE available_at<=?',(cutoff,)).fetchone()[0]
            result['collection_started_at']=c.execute('SELECT min(first_configured_at) FROM feeds WHERE first_configured_at<=?',(cutoff,)).fetchone()[0]
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError,OverflowError):
        # Never expose DB paths, configuration, raw records or exception details.
        result.update(status='unavailable',sources=[],news=[],recent=[])
    return result


def _news_list(items, cutoff):
    cards=[]
    for item in items:
        old=' · Outside selected 24h' if item['published_at']<=cutoff-WINDOW else ''
        cards.append('<article class="news-item"><p class="news-meta">'+escape(SOURCES[item['source_id']]+old)+'</p>'
            +'<h3><a href="'+escape(item['url'],quote=True)+'" target="_blank" rel="noopener noreferrer">'
            +escape(item['title'])+'</a></h3><p>'+escape(item['summary'])+'</p>'
            +'<p class="news-meta">Published '+stamp(item['published_at'])+'</p>'
            +'<details><summary>Acquisition history · '+str(item['versions'])+' version(s)</summary>'
            +'<dl class="news-provenance"><dt>First acquired</dt><dd>'+stamp(item['first_fetched_at'])+'</dd>'
            +'<dt>This version acquired</dt><dd>'+stamp(item['available_at'])+'</dd>'
            +'<dt>Last confirmed by cutoff</dt><dd>'+stamp(item['last_fetched_at'])+'</dd>'
            +'<dt>Archive version</dt><dd>'+str(item['version_id'])+'</dd></dl></details>'
            +'<p class="news-meta">Source: '+escape(item['attribution'])+'</p></article>')
    return ''.join(cards)


def render(data, historical=False, error=None):
    cutoff=data['as_of']
    content='<div class="news-heading"><div><h1>Phase 8pre · English news</h1>'
    content+='<p class="subtitle">Collection health and the news available at a chosen time.</p></div>'
    content+='<span class="news-status '+('news-historical' if historical else 'news-healthy')+'">'+('Historical snapshot' if historical else 'Live · refreshes every 60s')+'</span></div>'
    if error:
        content+='<p class="news-notice" role="alert">'+escape(error)+'</p>'
    options=''.join('<option value="'+key+'"'+(' selected' if data['source']==key else '')+'>'+escape(label)+'</option>'
                    for key,label in [('all','All public sources')]+[(k,SOURCES[k]) for k in PUBLIC_HOSTS])
    form='<form class="news-form" method="get" action="/phase8pre/">'
    form+='<label>As of (UTC)<input type="datetime-local" name="as_of" value="'+datetime.fromtimestamp(cutoff,timezone.utc).strftime('%Y-%m-%dT%H:%M')+'" required></label>'
    form+='<label>News source<select name="source">'+options+'</select></label>'
    form+='<button type="submit">Recreate snapshot</button><a href="/phase8pre/">Latest</a></form>'
    form+='<p class="muted">Cutoff: '+stamp(cutoff)+'. Only versions already acquired by this time, published in the preceding 24 hours.</p>'
    content+=card('Explore the archive',form)
    if data['status']!='ok':
        content+=card('News archive unavailable','<p>The archive cannot be read at the moment. Please try again later.</p>')
        return page_shell('Phase 8pre',content,'phase8pre',refresh=None if historical else 60)
    active=[s for s in data['sources'] if s['enabled']]
    healthy=sum(s['status']=='healthy' for s in active)
    content+=card('At this cutoff',metric_blocks([
        ('Available · last 24h',str(data['eligible_count'])+('+' if data['truncated'] else '')),
        ('Archived articles · all sources',data['archived_articles']),
        ('Article versions · all sources',data['archived_versions']),
        ('Healthy active feeds',str(healthy)+' / '+str(len(active)))]))
    table='<div class="table-scroll"><table><thead><tr><th>Source</th><th>State</th><th>Last successful check (UTC)</th><th>Archived</th><th>Checks / failures · 24h</th></tr></thead><tbody>'
    for s in data['sources']:
        label={'paused':'Permission pending' if s['id']=='coindesk' else 'Paused','healthy':'Healthy','attention':'Needs attention'}[s['status']]
        table+='<tr><td>'+escape(s['label'])+'</td><td><span class="news-status news-'+s['status']+'">'+label+'</span></td>'
        table+='<td>'+stamp(s['last_success_at'])+'</td><td>'+str(s['articles'])+'</td><td>'+str(s['successful_polls'])+' / '+str(s['failed_polls'])+'</td></tr>'
    table+='</tbody></table></div><p class="muted">Scheduled every 15 minutes. An unchanged RSS response counts as a successful check.</p>'
    if any(s['id']=='coindesk' and not s['enabled'] for s in data['sources']):
        table+='<p class="muted">CoinDesk was paused at this cutoff.</p>'
    content+=card('Source status',table)
    coverage='<p class="muted">Each mark is 15 minutes, from '+stamp(cutoff-WINDOW)+' to '+stamp(cutoff)+'.</p>'
    for s in active:
        coverage+='<div class="news-coverage-row"><strong>'+escape(s['label'])+'</strong><div class="news-timeline" role="img" aria-label="'+escape(s['label'])+': '+str(sum(b['state']=='observed' for b in s['bins']))+' of 96 intervals observed">'
        for b in s['bins']:
            coverage+='<span class="news-bin news-bin-'+b['state']+'" title="'+stamp(b['start'])+' → '+stamp(b['end'])+' · '+b['state']+'"></span>'
        coverage+='</div></div>'
    coverage+='<div class="news-legend"><span><i class="news-bin-observed"></i>Successful check</span><span><i class="news-bin-unobserved"></i>No successful check</span><span><i class="news-bin-before"></i>Before collection / paused</span></div>'
    gaps=[(s,g) for s in active for g in s['gaps']]
    if gaps:
        coverage+='<ul>'+''.join('<li>'+escape(s['label'])+': '+stamp(g['start'])+' → '+stamp(g['end'])+'</li>' for s,g in gaps[:20])+'</ul>'
    else:
        coverage+='<p>No extended polling gaps detected in the selected 24 hours.</p>'
    coverage+='<p class="muted">Gaps describe missing observations, not a known number of missing articles. A quiet news day can still have healthy collection.</p>'
    content+=card('Polling coverage · last 24h',coverage)
    news=_news_list(data['news'],cutoff) or '<p>No eligible news was published in this 24-hour window.</p><p class="muted">Older archived headlines appear below when available. Federal Reserve and BLS releases are not daily.</p>'
    if data['truncated']:
        news+='<p class="muted">Showing up to 100 recent matching articles; this public view is capped.</p>'
    content+=card('Available news · last 24h',news)
    if data['recent'] and not data['news']:
        content+=card('Recent archived headlines',_news_list(data['recent'],cutoff))
    query=urlencode(dict(as_of=format(cutoff,'.6f'),source=data['source']))
    content+='<p class="muted">RSS titles and summaries, with source attribution. Raw feeds and private configuration are not published.</p><p><a href="/phase8pre/data.json?'+escape(query,quote=True)+'">View this report as JSON</a></p>'
    return page_shell('Phase 8pre',content,'phase8pre',refresh=None if historical else 60)
