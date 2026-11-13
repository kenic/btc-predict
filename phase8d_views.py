"""Read-only Phase 8d UI. Public responses never expose requests or raw provider output."""
import json,time
from pathlib import Path
from datetime import datetime,timezone,timedelta
from html import escape
from phase8d import store,evaluation
from phase8d.core import ENGINES,SYMBOLS
from phase8pre2 import market
from ui import page_shell,styled_table,card
NAMES=dict(zip(ENGINES,('GPT-6 Astra','Claude Fable','Jev','Kairos')))
JST=timezone(timedelta(hours=9))
def stamp(t):return datetime.fromtimestamp(t,JST).strftime('%m/%d %H:%M:%S') if t is not None else '—'
def pct(v):return '—' if v is None else f'{v*100:.1f}%'
def decimal(v):return '—' if v is None else f'{v:.4f}'

def report(db,quotes_db,symbol='BTC-USD'):
    if symbol not in SYMBOLS:raise ValueError('unsupported_symbol')
    if not Path(db).exists():return {'status':'not_started','symbol':symbol,'records':[],'metrics':{},'health':[]}
    with store.connect(db,True) as c:
        c.execute('BEGIN');rows=c.execute('SELECT * FROM events ORDER BY cutoff,id').fetchall();records=[]
        calls={}
        for call in c.execute('SELECT event,engine,status,result_json FROM calls'):
            r=json.loads(call['result_json']) if call['result_json'] else {'status':call['status']}
            calls.setdefault(call['event'],{})[call['engine']]={k:r.get(k) for k in ('status','probability_up','requested_at','completed_at','latency_ms','error','cost_usd','cost_kind','news_set_id','input_id')}
        outcomes={r['event']:json.loads(r['result_json']) for r in c.execute('SELECT * FROM evaluations')}
        health=[dict(r) for r in c.execute('SELECT * FROM health')]
        policy=json.loads(c.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0])
        for row in rows:
            f=json.loads(row['input_json']) if row['input_json'] else None
            triggers=json.loads(row['trigger_json']);news=f['state']['news'] if f else []
            records.append(dict(id=row['id'],symbol=row['symbol'],kind=row['kind'],cutoff=row['cutoff'],target=row['target'],status=row['status'],error=row['error'],
              news=[{k:n.get(k) for k in ('article_id','version_id','title','source_id','published_at','available_at','is_trigger')} for n in news],
              trigger_count=len(triggers),trigger_lags_seconds=[row['cutoff']-n['published_at'] for n in triggers],
              news_set_id=f['state']['news_set_id'] if f else None,input_id=f['input_id'] if f else None,
              technical=f['state']['technical']['timeframes'] if f else None,
              reference_quote_observed_at=f['reference_quote']['observed'] if f else None,
              calls=calls.get(row['id'],{}),evaluation=outcomes.get(row['id'])))
    try:
        with market.connect(quotes_db,True) as c:health +=[dict(r) for r in c.execute('SELECT * FROM health')]
    except Exception:health.append(dict(worker='8pre2',observed=0,status='unavailable'))
    now=time.time()
    for h in health:h['stale']=now-h['observed']>(180 if h['worker']=='technical' else 30)
    return dict(status='ok',as_of=now,symbol=symbol,records=[r for r in records if r['symbol']==symbol][-100:][::-1],
       metrics=evaluation.aggregate(records),health=health,validation_events=sum(r['kind']=='validation' for r in records),
       live_events=sum(r['kind']=='live' for r in records),fee_bps_per_side=policy['fee_bps_per_side'],slippage_bps_per_side=policy['slippage_bps_per_side'])

def render(d):
    symbol=d['symbol'];body='<h1>Phase 8d · 新着ニュース＋テクニカル</h1><p>BTC / XRP · 同一入力を4エンジンで比較 · 実取引なし</p>'
    body+='<p><a href="?symbol=BTC-USD">BTC</a> · <a href="?symbol=XRP-USD">XRP</a></p>'
    body+=card('収集・実行状況',styled_table(['処理','状態','最終確認（日本時間）'],[[h['worker'],'更新遅延' if h['stale'] else h['status'],stamp(h['observed'])] for h in d['health']]))
    body+='<p>上昇：55%以上 ／ 下落：45%以下 ／ 見送り：45%超〜55%未満。確率は未校正です。入力不足・API失敗は見送りと別集計です。</p>'
    m=d.get('metrics',{}).get(symbol,{})
    for key,title in [('paired','予測成績：4エンジン共通で評価可能な全イベント（期間の重複あり）'),('paired_nonoverlap','予測成績：4時間間隔・非重複の共通評価')]:
        rows=[]
        for e,v in m.items():
            x=v[key];rows.append([NAMES[e],x['n'],x['judged'],pct(x['accuracy']),pct(x['abstention_rate']),decimal(x['brier']),decimal(x['logloss']),v['unavailable']])
        body+=card(title,styled_table(['エンジン','評価数','判断数','的中率','見送り率','Brier','LogLoss','予測不可'],rows))
    rows=[]
    for e,v in m.items():rows.append([NAMES[e],v['paper_event_count'],f"${v['paper_event_net_return_sum']*1000:.2f}",v['paper_portfolio_count'],f"${v['paper_portfolio_net_return_sum']*1000:.2f}",v['paper_portfolio_unavailable']])
    body+=card('ペーパートレード：回答後の価格から共通の4時間後まで',styled_table(['エンジン','イベント別取引数','イベント別損益合計','重複保有なし取引数','重複保有なし損益','価格不足'],rows))
    body+=f'<p>1取引1,000 USD固定・複利なし。実測スプレッドに加え、片道手数料 {d.get("fee_bps_per_side",5)} bps、片道スリッページ {d.get("slippage_bps_per_side",5)} bpsを仮定。売りは仮想ショートで借入費用は未算入。価格取得に15秒以上かかった取引は価格不足として除外します。</p>'
    for r in d['records'][:30]:
        label='接続確認・成績対象外' if r['kind']=='validation' else '新着イベント'
        content=f'<p>{label} · {escape(r["status"])} · 新着{r["trigger_count"]}件 · 入力{len(r["news"])}件'+(f' · {escape(r["error"])}' if r['error'] else '')+'</p>'
        content+='<p>公開から取得まで：'+', '.join(f'{x/60:.1f}分' for x in r['trigger_lags_seconds'])+'</p>'
        rr=[]
        for engine in ENGINES:
            c=r['calls'].get(engine,{});p=c.get('probability_up');decision=evaluation.decision(p) if p is not None else None
            o=r['evaluation'];correct='未評価'
            if o and o['price_up'] is not None and c.get('status')=='ok':correct='対象外（見送り）' if decision=='ABSTAIN' else '的中' if (decision=='UP')==bool(o['price_up']) else '外れ'
            if o and o['price_up'] is None:correct='価格不足'
            t=o['engines'].get(engine,{}) if o else {};profit=f"${t['net_return']*1000:.2f}" if t.get('status')=='executed' else t.get('status','未評価')
            rr.append([NAMES[engine],c.get('status','—'),pct(p),{'UP':'上昇','DOWN':'下落','ABSTAIN':'見送り'}.get(decision,'—'),stamp(c.get('completed_at')),correct,profit])
        content+=styled_table(['エンジン','応答','上昇確率','判断','予測完了','予測評価','仮想損益'],rr)
        content+='<details><summary>共通入力の記事・テクニカル</summary><ul>'+''.join('<li>'+escape(n['title'])+' ('+escape(str(n['source_id']))+f', 記事{n["article_id"]}/版{n["version_id"]})</li>' for n in r['news'])+'</ul>'
        content+='<pre>'+escape(json.dumps(r['technical'],ensure_ascii=False,indent=2))+'</pre></details>'
        body+=card(f'{symbol} · {stamp(r["cutoff"])} → {stamp(r["target"])}',content)
    body+='<p>旧実験：<a href="/phase8a/">8a</a> · <a href="/phase8b/">8b</a> · <a href="/phase8c/">8c</a>（新方式とは集計を分離）</p>'
    return page_shell('Phase 8d',body,'phase8d',refresh=30)
