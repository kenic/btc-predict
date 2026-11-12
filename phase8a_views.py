"""Public allowlisted projection of saved Phase 8a results. No experiment imports or I/O writes."""
import json
import math
import time
from datetime import datetime, timezone, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from ui import page_shell, card, metric_blocks, styled_table

SYMBOLS=('BTC-USD','ETH-USD','SOL-USD','XRP-USD','LINK-USD')
ENGINES=('gpt6_astra','claude_fable','jev','kairos')
LABELS=dict(zip(ENGINES,('GPT-6 Astra','Claude Fable','Jev','Kairos')))
STATUSES={'complete':'完了','partial':'一部失敗','preflight_skipped':'事前確認で見送り','reserved':'処理中または中断','ok':'成功','error':'エラー','late':'期限超過','skipped':'見送り','pending':'処理待ち','outside_start_window':'次の正時待ち','disabled':'停止設定','already_attempted_no_retry':'記録済み'}
JST=timezone(timedelta(hours=9))


def number(v):
    return v if type(v) in (int,float) and math.isfinite(v) else None


def stamp(v):
    try:return datetime.fromtimestamp(v,JST).strftime('%m/%d %H:%M') if number(v) is not None else '—'
    except (ValueError,OverflowError,OSError):return '—'


def read(path, limit=16*1024*1024):
    if path.is_symlink():raise ValueError('symlink_not_allowed')
    with path.open('rb') as f:data=f.read(limit+1)
    if len(data)>limit:raise ValueError('report_too_large')
    r=json.loads(data)
    if not isinstance(r,dict):raise ValueError('report_object_required')
    return r


def score(v):
    if not isinstance(v,dict):v={}
    return {k:number(v.get(k)) for k in ('n','accuracy','brier','logloss')}



def decision(p):
    if number(p) is None or not 0<=p<=1:return None
    return 'DOWN' if p<=.4 else 'UP' if p>=.6 else 'ABSTAIN'


def selective_scores(pairs):
    valid=[(p,y) for p,y in pairs if decision(p) is not None and type(y) is int and y in (0,1)]
    chosen=[(p,y) for p,y in valid if decision(p)!='ABSTAIN']
    n=len(valid);m=len(chosen);eps=1e-15
    return dict(n=n,decided_n=m,abstained_n=n-m,
        accuracy=sum((p>=.6)==bool(y) for p,y in chosen)/m if m else None,
        abstention_rate=(n-m)/n if n else None,
        brier=sum((p-y)**2 for p,y in valid)/n if n else None,
        logloss=-sum(y*math.log(max(eps,min(1-eps,p)))+(1-y)*math.log(max(eps,min(1-eps,1-p))) for p,y in valid)/n if n else None)


def selective_evaluation(answers,symbol):
    grouped={}
    for a in answers:
        if a.get('symbol')==symbol and a.get('engine') in ENGINES and type(a.get('cutoff')) is int:
            grouped.setdefault(a['cutoff'],{})[a['engine']]=a
    paired={t:r for t,r in grouped.items() if all(e in r and r[e].get('status')=='ok'
        and decision(r[e].get('probability_up')) is not None and type(r[e].get('actual_up')) is int
        and r[e]['actual_up'] in (0,1) for e in ENGINES)}
    return {mode:{e:selective_scores([(r[e]['probability_up'],r[e]['actual_up']) for t,r in paired.items()
        if mode=='hourly' or t%14400==0]) for e in ENGINES} for mode in ('nonoverlap','hourly')}


def selective_result(p,correct,valid=True):
    if not valid or decision(p) is None:return '対象外'
    if decision(p)=='ABSTAIN':return '見送り（採点対象外）'
    return '的中' if correct is True else '外れ' if correct is False else '未確定'


SELECTIVE_NOTE='<p>追加評価：40%以下は下落・横ばい寄り、60%以上は上昇寄り、40%超〜60%未満は見送り。的中率は判断した予測のみ、見送り率は答え合わせ済みの全有効予測が分母です。Brier・LogLossは見送りも含む全有効予測で評価します。API失敗・未確定は含めません。表示の丸め前の確率で判定します。</p>'
SELECTIVE_HEADERS=('エンジン','全評価件数','判断件数','見送り件数','的中率（見送り除外）','見送り率','Brier（全件）↓','LogLoss（全件）↓')


def selective_cells(s):
    return [s['n'],s['decided_n'],s['abstained_n'],pct(s['accuracy']),pct(s['abstention_rate']),dec(s['brier']),dec(s['logloss'])]

def _report(root, symbol='BTC-USD', cutoff=None, now=None):
    if symbol not in SYMBOLS:raise ValueError('invalid_symbol')
    if cutoff is not None and (not str(cutoff).isdigit() or len(str(cutoff))>11):raise ValueError('invalid_cutoff')
    now=time.time() if now is None else now
    root=Path(root)
    try:r=read(root/'report.json')
    except (OSError,ValueError):return {'status':'unavailable','symbol':symbol}
    try:p=read(root/'prediction-status.json')
    except (OSError,ValueError):p={}
    schedule=[{'cutoff':number(s.get('cutoff')),'status':s.get('status') if s.get('status') in STATUSES else 'pending',
               'calls':number(s.get('calls'))} for s in r.get('schedule',[]) if isinstance(s,dict)]
    if number(p.get('cutoff')) and not any(s['cutoff']==p['cutoff'] for s in schedule):
        schedule.append({'cutoff':p['cutoff'],'status':p.get('status') if p.get('status') in STATUSES else 'pending','calls':None})
    schedule=sorted((s for s in schedule if s['cutoff'] is not None),key=lambda s:s['cutoff'],reverse=True)[:72]
    selected=int(cutoff) if cutoff is not None else (int(schedule[0]['cutoff']) if schedule else None)
    if cutoff is not None and selected not in {s['cutoff'] for s in schedule}:raise ValueError('unknown_cutoff')
    out={'status':'ok','symbol':symbol,'as_of':number(r.get('as_of')),'stale':number(r.get('as_of')) is None or now-r['as_of']>300,
         'enabled':r.get('prediction_enabled') is True,'latest_status':p.get('status') if p.get('status') in STATUSES else 'pending',
         'selected_cutoff':selected,'target_end':selected+14400 if selected is not None else None,
         'history':schedule,'assets':{},'predictions':[], 'answers':[], 'news_count':None,'direct_news_count':None,
         'known_cost_usd':None,'unknown_cost_calls':None,'paper':{}}
    for s in SYMBOLS:
        a=r.get('assets',{}).get(s,{})
        out['assets'][s]={'recorded':number(a.get('recorded')),'evaluated':number(a.get('evaluated')),'engines':{}}
        for e in ENGINES:
            v=a.get('engines',{}).get(e,{})
            out['assets'][s]['engines'][e]={k:score(v.get(k)) for k in ('paired_hourly_descriptive','paired_nonoverlap_primary')}
    if selected is not None:
        path=root/'runs'/str(selected)
        try:
            if path.is_symlink():raise ValueError('symlink_not_allowed')
            summary=read(path/'summary.json')
            a=summary.get('assets',{}).get(symbol,{})
            out.update(news_count=number(a.get('news_count')),direct_news_count=number(a.get('direct_news_count')),
                       known_cost_usd=number(summary.get('known_cost_estimate_usd')),unknown_cost_calls=number(summary.get('unknown_cost_calls')))
            for e in ENGINES:
                paper=a.get('evaluation',{}).get('engines',{}).get(e,{}).get('paper',{})
                out['paper'][e]={k:number(paper.get(k)) for k in ('opportunities','trades','skip_rate','pnl_after_trading_cost_usd')}
            for v in a.get('results',[]):
                if v.get('engine') not in ENGINES:continue
                prob=number(v.get('probability_up'))
                if prob is not None and not 0<=prob<=1:prob=None
                out['predictions'].append({'engine':v['engine'],'status':v.get('status') if v.get('status') in STATUSES else 'pending',
                    'probability_up':prob,'direction':'UP' if prob is not None and prob>=.5 else 'DOWN' if prob is not None else None,
                    'decision':decision(prob) if v.get('status')=='ok' else None,
                    'latency_ms':number(v.get('latency_ms'))})
        except (OSError,ValueError):pass
    # Allowlisted values only: never publish provider raw, request, input news, config or file paths.
    for v in r.get('answers',[]):
        if v.get('symbol')!=symbol or v.get('engine') not in ENGINES:continue
        row={k:number(v.get(k)) for k in ('cutoff','target_end','probability_up','reference_price','end_price','brier','logloss')}
        row.update(engine=v['engine'],actual_up=v.get('actual_up') if type(v.get('actual_up')) is int and v['actual_up'] in (0,1) else None,
                   correct=v.get('correct') if type(v.get('correct')) is bool else None,
                   decision=decision(v.get('probability_up')) if v.get('status')=='ok' else None)
        out['answers'].append(row)
    out['selective_evaluation']=selective_evaluation(r.get('answers',[]),symbol)
    out['answers']=sorted(out['answers'],key=lambda a:a['cutoff'] or 0,reverse=True)[:80]
    return out


def report(root, symbol='BTC-USD', cutoff=None, now=None):
    try:return _report(root,symbol,cutoff,now)
    except (OSError,TypeError,AttributeError,KeyError,OverflowError):
        return {'status':'unavailable','symbol':symbol if symbol in SYMBOLS else 'BTC-USD'}


def pct(v):return '—' if v is None else f'{v*100:.1f}%'
def dec(v):return '—' if v is None else f'{v:.4f}'


def render(r):
    symbol=r['symbol'];content='<h1>Phase 8a · ニュース予測比較</h1><p class="subtitle">BTC・ETH・SOL・XRP・LINK / 4エンジン / 毎時予測・4時間後に答え合わせ。実取引なし。</p>'
    content+='<nav class="filters" aria-label="対象銘柄">'+''.join('<a href="/phase8a/?'+urlencode({'symbol':s})+'"'+(' aria-current="page"' if s==symbol else '')+'>'+s.split('-')[0]+'</a>' for s in SYMBOLS)+'</nav>'
    if r['status']!='ok':
        content+=card('データ待ち','<p>保存済みの集計をまだ読み取れません。画面から予測を起動することはありません。</p>')
        return page_shell('Phase 8a',content,'phase8a',refresh=60).replace('<html lang="en">','<html lang="ja">')
    content+='<p class="muted">表示時刻は日本時間（JST）。照合レポート更新：'+stamp(r['as_of'])+'。60秒ごとに画面を更新します。</p>'
    if r['stale']:content+=card('更新が遅れています','<p>集計が5分以上更新されていないか、更新時刻が不明です。保存済みの値を表示しています。</p>')
    content+=card('稼働状況',metric_blocks([('予測の設定','有効' if r['enabled'] else '無効'),('直近処理',STATUSES[r['latest_status']]),('記録済み時刻',r['assets'][symbol]['recorded']),('答え合わせ済み',r['assets'][symbol]['evaluated'])])+'<p class="muted">設定と保存済み記録を表示しています。サービスの現在の稼働を保証する表示ではありません。</p>')
    form='<form method="get" action="/phase8a/"><input type="hidden" name="symbol" value="'+symbol+'"><label for="cohort">予測時刻（JST）</label> <select id="cohort" name="cutoff">'
    form+=''.join('<option value="'+str(int(s['cutoff']))+'"'+(' selected' if s['cutoff']==r['selected_cutoff'] else '')+'>'+stamp(s['cutoff'])+' · '+STATUSES[s['status']]+'</option>' for s in r['history'])
    content+=form+'</select> <button type="submit">表示</button></form>'
    answers={a['engine']:a for a in r['answers'] if a['cutoff']==r['selected_cutoff']}
    pred={v['engine']:v for v in r['predictions']};rows=[]
    for e in ENGINES:
        v=pred.get(e,{});a=answers.get(e)
        direction={'ABSTAIN':'見送り','UP':'上昇寄り','DOWN':'下落・横ばい寄り'}.get(v.get('decision'),'—')
        result=('的中' if a['correct'] else '外れ') if a and a['correct'] is not None else ('価格照合待ち' if r['target_end'] is not None and time.time()>=r['target_end']+60 else '4時間後を待機') if v.get('status')=='ok' else '予測なし'
        rows.append([LABELS[e],pct(v.get('probability_up')),direction,STATUSES.get(v.get('status'),'未記録'),selective_result(v.get('probability_up'),a.get('correct') if a else None,v.get('status')=='ok'),result,'—' if v.get('latency_ms') is None else f"{v['latency_ms']/1000:.2f}秒"])
    title=symbol+' · '+stamp(r['selected_cutoff'])+' → '+stamp(r['target_end'])
    content+=card(title,styled_table(('エンジン','上昇確率','判断（40/60%）','応答','答え合わせ（見送り除外）','従来の答え合わせ（50%境界）','応答時間'),rows)+'<p class="muted">確率は未校正です。見送りは値動きの予測ではなく方向判断を控えることです。従来評価では50%以上をUPとして保存しています。Kairos confidenceを勝率には使っていません。</p>')
    content+=card('入力ニュースと費用',metric_blocks([('選択記事数',r['news_count']),('銘柄に直接関連',r['direct_news_count']),('今回の5銘柄・既知料金推定','—' if r['known_cost_usd'] is None else f"${r['known_cost_usd']:.4f}")])+'<p>同一銘柄では4エンジンへ同じニュースを入力。直接記事が0件の場合は市場共通ニュースによる予測です。記事本文・生回答は公開しません。Kairos料金は不明で、表示額は総請求額ではありません。</p>')
    content+=SELECTIVE_NOTE
    for mode,title in [('nonoverlap','追加評価：見送りあり・4時間非重複'),('hourly','追加参考評価：見送りあり・毎時（期間重複）')]:
        content+=card(title,styled_table(SELECTIVE_HEADERS,[[LABELS[e]]+selective_cells(r['selective_evaluation'][mode][e]) for e in ENGINES]))
    for mode,title in [('paired_nonoverlap_primary','従来評価（50%境界）：4時間ごとの非重複予測'),('paired_hourly_descriptive','従来参考評価（50%境界）：毎時の予測（評価期間が重複）')]:
        rows=[]
        for e in ENGINES:
            s=r['assets'][symbol]['engines'][e][mode];rows.append([LABELS[e],s['n'] or 0,pct(s['accuracy']),dec(s['brier']),dec(s['logloss'])])
        content+=card(title,styled_table(('エンジン','評価件数','Accuracy','Brier ↓','LogLoss ↓'),rows)+'<p class="muted">4エンジンの有効回答と実現価格がそろった時刻で比較。0件は未評価で、成績ゼロではありません。少数標本から優劣は判断できません。</p>')
    if r['paper']:
        content+=card('選択時刻のペーパートレード',styled_table(('エンジン','評価機会','取引数','見送り率','損益 USD（API費用除外）'),[[LABELS[e],v['opportunities'] or 0,v['trades'] or 0,pct(v['skip_rate']),'—' if not v['opportunities'] else dec(v['pnl_after_trading_cost_usd'])] for e,v in r['paper'].items()])+'<p class="muted">4時間非重複の対象時刻のみ。売買コスト控除後、API費用控除前の仮想損益です。実取引は行いません。</p>')
    content+=card('最近の答え合わせ',styled_table(('予測 JST','銘柄','エンジン','上昇確率','実際','結果（見送り除外）','従来結果（50%境界）','開始価格 → 終了価格'),[[stamp(a['cutoff']),symbol,LABELS[a['engine']],pct(a['probability_up']),'上昇' if a['actual_up']==1 else '下落・横ばい' if a['actual_up']==0 else '—',selective_result(a['probability_up'],a['correct'],a.get('decision') is not None),'的中' if a['correct'] is True else '外れ' if a['correct'] is False else '対象外',('—' if a['reference_price'] is None else f"{a['reference_price']:,.4f}")+' → '+('—' if a['end_price'] is None else f"{a['end_price']:,.4f}")] for a in r['answers']]) if r['answers'] else '<p>まだ4時間後の答え合わせは確定していません。</p>')
    content+=card('直近の実行履歴',styled_table(('予測 JST','予定照合 JST','状態','呼出し予約件数'),[[stamp(s['cutoff']),stamp(s['cutoff']+14460),STATUSES[s['status']],s['calls'] if s['calls'] is not None else '—'] for s in r['history'][:24]]))
    content+='<p><a href="/phase8a/data.json?'+escape(urlencode({'symbol':symbol,**({'cutoff':r['selected_cutoff']} if r['selected_cutoff'] is not None else {})}),quote=True)+'">公開用データ JSON</a></p>'
    return page_shell('Phase 8a',content,'phase8a',refresh=60).replace('<html lang="en">','<html lang="ja">')
