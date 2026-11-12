"""Read-only 8a/8b comparison; aggregate only genuinely matched observations."""
import math
from pathlib import Path
from urllib.parse import urlencode
import phase8a_views as base
from ui import page_shell, card, styled_table


def summary(root, cutoff, symbol):
    directory=Path(root)/'runs'/str(cutoff)
    if directory.is_symlink():raise ValueError('symlink_not_allowed')
    return base.read(directory/'summary.json')['assets'][symbol]


def matched(a, b, symbol, roots):
    grouped=[]
    for r in (a,b):
        g={}
        for v in r.get('answers',[]):
            if v.get('symbol')==symbol and v.get('engine') in base.ENGINES:
                g.setdefault(v.get('cutoff'),{})[v['engine']]=v
        grouped.append(g)
    result=[]
    for t in sorted(set(grouped[0])&set(grouped[1])):
        if type(t) is not int or t<0:continue
        x,y=grouped[0][t],grouped[1][t]
        try:
            for e in base.ENGINES:
                for v in (x[e],y[e]):
                    if v.get('status')!='ok' or base.number(v.get('probability_up')) is None or not 0<=v['probability_up']<=1:
                        raise ValueError('invalid_prediction')
                    if type(v.get('actual_up')) is not int or v['actual_up'] not in (0,1):raise ValueError('invalid_outcome')
                if any(x[e].get(k)!=y[e].get(k) for k in ('reference_price','end_price','actual_up')):
                    raise ValueError('different_outcomes')
            sa,sb=[summary(root,t,symbol) for root in roots]
            if not sa.get('news_set_id') or sa['news_set_id']!=sb.get('news_set_id'):continue
        except (OSError,ValueError,KeyError,TypeError):continue
        result.append((t,x,y))
    return result


def scores(pairs):
    n=len(pairs)
    if not n:return dict(n=0,accuracy=None,brier=None,logloss=None)
    return dict(n=n,accuracy=sum((p>=.5)==bool(y) for p,y in pairs)/n,
                brier=sum((p-y)**2 for p,y in pairs)/n,
                logloss=-sum(y*math.log(max(1e-15,min(1-1e-15,p)))+(1-y)*math.log(max(1e-15,min(1-1e-15,1-p))) for p,y in pairs)/n)


def report(root_a, root_b, symbol='BTC-USD', cutoff=None):
    b=base.report(root_b,symbol,cutoff)
    selected=b.get('selected_cutoff')
    try:a=base.report(root_a,symbol,selected) if selected is not None else {'status':'unavailable','symbol':symbol}
    except ValueError:a={'status':'unavailable','symbol':symbol}
    output={'phase8a':a,'phase8b':b,'paired':{},'paired_selective':{},'technical':{},'same_news':False}
    try:
        sa,sb=[summary(root,selected,symbol) for root in (root_a,root_b)]
        output['same_news']=bool(sa.get('news_set_id')) and sa['news_set_id']==sb.get('news_set_id')
        # Public numeric projection, never arbitrary model strings or archive contents.
        output['technical']={k:base.number(sb.get('technical',{}).get(k)) for k in (
            'latest_close_at','bars','bar_seconds','close_usd','sma20_usd','sma50_usd','rsi14','macd_usd',
            'macd_signal_usd','atr14_usd','return_1h','return_4h','volume_ratio20')}
    except (OSError,ValueError,KeyError,TypeError,AttributeError):pass
    try:
        raw_a,raw_b=[base.read(Path(root)/'report.json') for root in (root_a,root_b)]
        rows=matched(raw_a,raw_b,symbol,(root_a,root_b))
    except (OSError,ValueError,KeyError,TypeError,AttributeError):rows=[]
    for mode in ('nonoverlap','hourly'):
        selected_rows=[r for r in rows if mode=='hourly' or r[0]%14400==0]
        output['paired_selective'][mode]={e:{label:base.selective_scores([(r[i][e]['probability_up'],r[i][e]['actual_up']) for r in selected_rows])
            for i,label in ((1,'phase8a'),(2,'phase8b'))} for e in base.ENGINES}
        output['paired'][mode]={e:{label:scores([(r[i][e]['probability_up'],r[i][e]['actual_up']) for r in selected_rows])
            for i,label in ((1,'phase8a'),(2,'phase8b'))} for e in base.ENGINES}
    return output


def render(data):
    a,b=data['phase8a'],data['phase8b'];symbol=b['symbol']
    main=base.render(b).split('<main>',1)[1].split('</main>',1)[0]
    main=main.replace('/phase8a/','/phase8b/').replace('Phase 8a · ニュース予測比較','Phase 8b · テクニカル＋ニュース')
    main=main.replace('主評価：4時間ごとの非重複予測','8b単独の主評価：4時間ごとの非重複予測').replace('参考評価：毎時の予測（評価期間が重複）','8b単独の参考評価：毎時の予測（評価期間が重複）')
    intro='<p><a href="/phase8a/?'+urlencode({'symbol':symbol})+'">8a：ニュースのみの詳細</a> ／ 8b：テクニカル＋ニュース。同じ予測時刻・同じ記事で比較します。</p>'
    if b.get('status')=='ok':
        if a.get('stale') or a.get('status')!='ok':intro+='<p>8aの比較データが未取得、または更新が遅れています。</p>'
        pred_a={v['engine']:v for v in a.get('predictions',[])};pred_b={v['engine']:v for v in b.get('predictions',[])}
        answers=[{v['engine']:v for v in r.get('answers',[]) if v['cutoff']==b['selected_cutoff']} for r in (a,b)]
        rows=[]
        for e in base.ENGINES:
            result=lambda d,p:base.selective_result(p.get('probability_up'),d.get('correct'),p.get('status')=='ok')
            rows.append([base.LABELS[e],base.pct(pred_a.get(e,{}).get('probability_up')),base.pct(pred_b.get(e,{}).get('probability_up')),
                         result(answers[0].get(e,{}),pred_a.get(e,{})),result(answers[1].get(e,{}),pred_b.get(e,{}))])
        intro+=card('同時刻比較 · '+base.stamp(b['selected_cutoff']),styled_table(('エンジン','8a 上昇確率','8b 上昇確率','8a 答え合わせ（見送り除外）','8b 答え合わせ（見送り除外）'),rows)+
                    ('<p>同一ニュース集合を確認済み。</p>' if data['same_news'] else '<p>両条件の同一ニュースは未確認です。比較成績には算入しません。</p>'))
    intro+=base.SELECTIVE_NOTE
    for mode,title in (('nonoverlap','8a vs 8b 追加評価：見送りあり・4時間非重複'),('hourly','8a vs 8b 追加参考評価：見送りあり・毎時（期間重複）')):
        rows=[]
        for e,v in data['paired_selective'][mode].items():
            for label in ('phase8a','phase8b'):
                rows.append([base.LABELS[e],label.replace('phase','')]+base.selective_cells(v[label]))
        intro+=card(title,styled_table(('エンジン','条件')+base.SELECTIVE_HEADERS[1:],rows)+'<p>同一ニュース・実現価格かつ全8回答成功の共通時刻が対象です。判断件数はエンジン・条件によって異なります。</p>')
    for mode,title in (('nonoverlap','8a vs 8b 従来評価（50%境界）：共通時刻・4時間非重複'),('hourly','従来参考評価（50%境界）：共通時刻・毎時（期間重複）')):
        rows=[]
        for e,v in data['paired'][mode].items():
            for label in ('phase8a','phase8b'):
                s=v[label];rows.append([base.LABELS[e],label.replace('phase',''),s['n'],base.pct(s['accuracy']),base.dec(s['brier']),base.dec(s['logloss'])])
        intro+=card(title,styled_table(('エンジン','条件','件数','Accuracy','Brier ↓','LogLoss ↓'),rows)+'<p>両条件の全4エンジンが成功し、ニュース集合・実現価格が一致した時刻のみ。少数標本では優劣を判断できません。</p>')
    if data['technical']:
        t=data['technical'];intro+=card('8bに渡したテクニカル指標',styled_table(('項目','値'),[(k,base.stamp(v) if k=='latest_close_at' else base.dec(v)) for k,v in t.items()])+'<p>確定5分足200本。最新足は予測時刻の5分前に終了し、取得時刻も予測時刻以前のものだけを使用します。指標不足時は8bを見送ります。</p>')
    # Put the comparison after the symbol/time selectors and before the detail cards.
    marker='</form>'
    if marker in main:main=main.replace(marker,marker+intro,1)
    else:main+=intro
    return page_shell('Phase 8b / 8a比較',main,'phase8b',refresh=60).replace('<html lang="en">','<html lang="ja">')
