"""Read-only 8a/8b/8c comparison, retaining matched denominators and legacy scores."""
from pathlib import Path
from urllib.parse import urlencode
import phase8a_views as base
from phase8b_views import matched,scores,summary
from ui import page_shell,card,styled_table


def report(root_a,root_b,root_c,symbol='BTC-USD',cutoff=None):
    c=base.report(root_c,symbol,cutoff);selected=c.get('selected_cutoff')
    roots={'phase8a':root_a,'phase8b':root_b,'phase8c':root_c}
    data={'phase8c':c,'comparisons':{},'same_news':False,'technical':{}}
    for label,root in list(roots.items())[:2]:
        try:data[label]=base.report(root,symbol,selected) if selected is not None else dict(status='unavailable',symbol=symbol)
        except ValueError:data[label]=dict(status='unavailable',symbol=symbol)
        try:rows=matched(base.read(Path(root)/'report.json'),base.read(Path(root_c)/'report.json'),symbol,(root,root_c))
        except (OSError,ValueError,KeyError,TypeError,AttributeError):rows=[]
        rows=[r for r in rows if all(v[e].get('target_end')==r[0]+14400 for v in r[1:] for e in base.ENGINES)]
        data['comparisons'][label]={}
        for mode in ('hourly','nonoverlap'):
            subset=[r for r in rows if mode=='hourly' or r[0]%14400==0]
            data['comparisons'][label][mode]={e:{name:{'legacy':scores([(r[i][e]['probability_up'],r[i][e]['actual_up']) for r in subset]),
                'selective':base.selective_scores([(r[i][e]['probability_up'],r[i][e]['actual_up']) for r in subset])}
                for i,name in ((1,label),(2,'phase8c'))} for e in base.ENGINES}
    try:
        ss=[summary(root,selected,symbol) for root in roots.values()]
        data['same_news']=bool(ss[0].get('news_set_id')) and len({s.get('news_set_id') for s in ss})==1
        t=ss[-1].get('technical') or {}
        data['technical']={'timeframes':{frame:{k:base.number(v.get(k)) for k in ('latest_close_at','bars','close_usd','sma20','sma50','rsi14','macd','macd_signal','macd_hist','bb_position','volume_ratio')}
                           for frame,v in t.get('timeframes',{}).items() if frame in ('300','900','3600')},
                           'micro_latest_observed_at':base.number(t.get('microstructure',{}).get('latest_observed_at'))}
    except (OSError,ValueError,KeyError,TypeError,AttributeError):pass
    return data


def render(data):
    c=data['phase8c'];symbol=c['symbol']
    main=base.render(c).split('<main>',1)[1].split('</main>',1)[0]
    main=main.replace('/phase8a/','/phase8c/').replace('Phase 8a · ニュース予測比較','Phase 8c · Phase 6相当テクニカル＋ニュース')
    intro='<p><a href="/phase8a/?'+urlencode({'symbol':symbol})+'">8a：ニュースのみ</a> ／ <a href="/phase8b/?'+urlencode({'symbol':symbol})+'">8b：簡易テクニカル＋ニュース</a> ／ 8c：複数時間足・板・売買フロー＋ニュース</p>'
    intro+='<p>毎時の同一時刻を基準に4時間後を予測。モデル・推論設定・ニュース選別は8bと共通です。取得済みデータのみ使用し、必要な履歴が不足する場合は8cをスキップします。</p>'
    if c.get('status')=='ok' and c.get('selected_cutoff') is not None:
        rows=[]
        for e in base.ENGINES:
            row=[base.LABELS[e]]
            for label in ('phase8a','phase8b','phase8c'):
                d=data[label];pred=next((v for v in d.get('predictions',[]) if v['engine']==e),{})
                answer=next((v for v in d.get('answers',[]) if v['engine']==e and v['cutoff']==c['selected_cutoff']),{})
                row.extend([base.pct(pred.get('probability_up')),base.selective_result(pred.get('probability_up'),answer.get('correct'),pred.get('status')=='ok')])
            rows.append(row)
        intro+=card('同時刻比較 · '+base.stamp(c['selected_cutoff']),styled_table(('エンジン','8a 上昇確率','8a 答え合わせ','8b 上昇確率','8b 答え合わせ','8c 上昇確率','8c 答え合わせ'),rows)+('<p>3条件の同一ニュース集合を確認済み。</p>' if data['same_news'] else '<p>3条件の同一ニュースは未確認です。成績は以下の一致確認済みペアのみで比較します。</p>'))
    else:intro+='<p>8cの初回予測を準備中、または入力不足により見送り中です。過去時刻の予測を後から補完しません。</p>'
    intro+=base.SELECTIVE_NOTE
    for label in ('phase8a','phase8b'):
        for mode,title in (('nonoverlap','主評価：4時間非重複'),('hourly','参考：毎時・評価期間重複')):
            rows=[];legacy=[]
            for e,values in data['comparisons'][label][mode].items():
                for name in (label,'phase8c'):
                    v=values[name];rows.append([base.LABELS[e],name.replace('phase','')]+base.selective_cells(v['selective']))
                    s=v['legacy'];legacy.append([base.LABELS[e],name.replace('phase',''),s['n'],base.pct(s['accuracy']),base.dec(s['brier']),base.dec(s['logloss'])])
            intro+=card(label.replace('phase','')+' vs 8c · '+title,styled_table(('エンジン','条件')+base.SELECTIVE_HEADERS[1:],rows)+'<p>同一ニュース・同じ実現価格・全8回答成功の共通時刻が対象。Brier・LogLossには見送りも含めます。比較ペアごとに対象件数は異なります。</p>')
            intro+=card('従来評価（50%境界） · '+label.replace('phase','')+' vs 8c · '+title,styled_table(('エンジン','条件','件数','Accuracy','Brier ↓','LogLoss ↓'),legacy))
    intro+=card('入力と時点の扱い','<p>各200本の1時間・15分・5分足、直近12本の5分足、板と売買フローの1・5・15・60分集計。確定・取得を予測時刻より前に揃えるため、最新足の終了はそれぞれ1時間・15分・5分前です。Phase 6の値動き選別は行いません。</p>')
    marker='</form>'
    main=main.replace(marker,marker+intro,1) if marker in main else intro+main
    return page_shell('Phase 8c / 8a・8b比較',main,'phase8c',refresh=60).replace('<html lang="en">','<html lang="ja">')
