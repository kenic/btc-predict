"""Separate four-hour direction scores from executable-quote paper returns."""
import json,math,time
from . import store
from phase8pre2 import market
from .core import canonical,ENGINES,SYMBOLS

def decision(p):return 'UP' if p>=.55 else 'DOWN' if p<=.45 else 'ABSTAIN'

def paper(result,entry,exit_quote,fee_bps,slippage_bps):
    side=decision(result['probability_up'])
    if side=='ABSTAIN':return {'status':'abstain'}
    if entry is None or exit_quote is None:return {'status':'price_unavailable'}
    slip=slippage_bps/10000;fee=fee_bps/10000
    if side=='UP':
        buy=entry['ask']*(1+slip);sell=exit_quote['bid']*(1-slip);gross=(sell-buy)/buy;fees=fee*(buy+sell)/buy
        entry_price,exit_price=buy,sell
    else:
        sell=entry['bid']*(1-slip);buy=exit_quote['ask']*(1+slip);gross=(sell-buy)/sell;fees=fee*(buy+sell)/sell
        entry_price,exit_price=sell,buy
    return dict(status='executed',side=side,entry_quote_id=entry['id'],exit_quote_id=exit_quote['id'],
      entry_at=entry['observed'],exit_at=exit_quote['observed'],entry_price=entry_price,exit_price=exit_price,
      gross_return=gross,fee_return=fees,net_return=gross-fees,short_is_synthetic=side=='DOWN')

def evaluate(db,policy,clock=time.time):
    now=clock();saved=0
    with market.connect(policy['quotes_db'],True) as prices, store.connect(db) as c,c:
        rows=c.execute("SELECT * FROM events WHERE target+20<=? AND input_json IS NOT NULL AND id NOT IN (SELECT event FROM evaluations)",(now,)).fetchall()
        for e in rows:
            ref=prices.execute('SELECT * FROM quotes WHERE id=?',(e['reference_quote'],)).fetchone();end=market.after(prices,e['symbol'],e['target'])
            outcome=dict(status='ok' if ref and end else 'price_unavailable',reference_quote_id=e['reference_quote'],end_quote_id=end['id'] if end else None,price_up=None,engines={})
            if ref and end:outcome.update(price_up=int(market.midpoint(end)>market.midpoint(ref)),reference_price=market.midpoint(ref),end_price=market.midpoint(end),end_observed_at=end['observed'])
            for call in c.execute('SELECT * FROM calls WHERE event=?',(e['id'],)).fetchall():
                r=json.loads(call['result_json']) if call['result_json'] else None
                if not r or r['status']!='ok':outcome['engines'][call['engine']]={'status':'prediction_unavailable'};continue
                if r['completed_at']>=e['target']:outcome['engines'][call['engine']]={'status':'completed_after_target'};continue
                entry=market.after(prices,e['symbol'],r['completed_at'])
                if entry and entry['observed']>=e['target']:entry=None
                outcome['engines'][call['engine']]=paper(r,entry,end,policy['fee_bps_per_side'],policy['slippage_bps_per_side'])
            c.execute('INSERT INTO evaluations VALUES(?,?)',(e['id'],canonical(outcome)));saved+=1
    return saved

def scores(pairs):
    if not pairs:return dict(n=0,judged=0,accuracy=None,abstention_rate=None,brier=None,logloss=None)
    judged=[(p,y) for p,y in pairs if decision(p)!='ABSTAIN']
    return dict(n=len(pairs),judged=len(judged),accuracy=sum((p>=.55)==bool(y) for p,y in judged)/len(judged) if judged else None,
      abstention_rate=1-len(judged)/len(pairs),brier=sum((p-y)**2 for p,y in pairs)/len(pairs),
      logloss=-sum(y*math.log(max(1e-15,p))+(1-y)*math.log(max(1e-15,1-p)) for p,y in pairs)/len(pairs))

def aggregate(records):
    output={}
    for symbol in SYMBOLS:
        rows=sorted([r for r in records if r['symbol']==symbol and r['kind']=='live'],key=lambda r:(r['cutoff'],r['id']))
        chosen=set();next_at=0
        for r in rows:
            if r['cutoff']>=next_at:chosen.add(r['id']);next_at=r['target']
        output[symbol]={}
        for engine in ENGINES:
            pairs=[];nonoverlap=[];paired=[];paired_nonoverlap=[];trades=[];portfolio=[];busy_until=0;valid=0
            for r in rows:
                result=r['calls'].get(engine);o=r.get('evaluation');ok=result and result['status']=='ok'
                valid+=bool(ok)
                if not ok or not o:continue
                if o['price_up'] is not None:
                    pair=(result['probability_up'],o['price_up']);pairs.append(pair)
                    if r['id'] in chosen:nonoverlap.append(pair)
                    if all(r['calls'].get(e,{}).get('status')=='ok' for e in ENGINES):
                        paired.append(pair)
                        if r['id'] in chosen:paired_nonoverlap.append(pair)
                t=o['engines'].get(engine,{})
                if t.get('status')=='executed':trades.append(t)
            # Each engine uses its own executable entry time, not news ordering.
            # A missing exit blocks a position through its fixed target + quote tolerance.
            candidates=[]
            for r in rows:
                res=r['calls'].get(engine);o=r.get('evaluation')
                if not res or res['status']!='ok' or decision(res['probability_up'])=='ABSTAIN' or not o:continue
                t=o['engines'].get(engine,{})
                candidates.append((t.get('entry_at',res['completed_at']),r,t))
            unavailable_positions=0
            for entry_at,r,t in sorted(candidates,key=lambda x:(x[0],x[1]['id'])):
                if entry_at<busy_until:continue
                busy_until=t.get('exit_at',r['target']+15)
                if t.get('status')=='executed':portfolio.append(t)
                else:unavailable_positions+=1
            output[symbol][engine]=dict(events=len(rows),valid_predictions=valid,unavailable=len(rows)-valid,
              all_events=scores(pairs),nonoverlap=scores(nonoverlap),paired=scores(paired),paired_nonoverlap=scores(paired_nonoverlap),
              paper_event_count=len(trades),paper_event_net_return_sum=sum(t['net_return'] for t in trades),
              paper_portfolio_count=len(portfolio),paper_portfolio_net_return_sum=sum(t['net_return'] for t in portfolio),
              paper_portfolio_unavailable=unavailable_positions)
    return output
