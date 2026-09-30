"""Prediction entry point, preserving the Phase 3 market representation."""
import json
import sqlite3
from datetime import datetime, timezone
from phase4 import ROOT, DB_PATH, load_config, save_prediction
from volatility import CLASSES, classify, probabilities, realized_volatility

def instructions(config):
    return ("Predict next completed 1h BTC-USD realized volatility using ONLY the supplied "
            "Phase 3 inputs (1h/15m/5m technicals, order book, aggressive trade flow). "
            "All observations precede the target hour. Do not use news, external knowledge or future data. "
            "RV = 100 * sqrt(sum of 12 squared consecutive 5m close-to-close log returns), "
            "including the return from the preceding 5m close to the first target close. "
            f"QUIET: RV < {config['quiet_upper']:.17g}%; NORMAL: "
            f"{config['quiet_upper']:.17g}% <= RV < {config['active_lower']:.17g}%; "
            f"ACTIVE: RV >= {config['active_lower']:.17g}%. "
            "Predict magnitude, not direction. Order book imbalance 0.5 is balanced; "
            "buy_ratio above 0.5 indicates aggressive buyers; microprice offset is microprice minus midpoint.")

def run(predictor):
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    config=load_config(require_enabled=True)
    # Reuse the existing fetching, filtering and indicator functions unchanged.
    import predict_gpt as source
    from indicators import calculate_timeframe_indicators, make_multitimeframe_snapshot
    from microstructure import make_microstructure_snapshot
    cutoff=source.get_prediction_cutoff()
    if cutoff < config['calibration_end']:
        raise RuntimeError('Target overlaps calibration history')
    if datetime.now(timezone.utc).timestamp()-cutoff > 600:
        raise RuntimeError('Prediction is more than 10 minutes late; refusing hindsight')
    with sqlite3.connect(DB_PATH) as c:
        if c.execute('SELECT 1 FROM volatility_predictions WHERE target_candle_time=? AND predictor=?',(cutoff,predictor)).fetchone():
            print('Prediction already exists; skipping')
            return
        if c.execute('SELECT count(*) FROM volatility_predictions WHERE predictor=?',(predictor,)).fetchone()[0]>=config['prediction_limit']:
            print('Phase 4 limit reached; skipping')
            return
    source.validate_microstructure(cutoff)
    candles=[source.remove_after_cutoff(source.get_candles(g),g,cutoff) for g in (3600,900,300)]
    if any(len(x)<50 for x in candles):
        raise RuntimeError('Not enough completed candles')
    if any(int(x[-1][0])+g!=cutoff for x,g in zip(candles,(3600,900,300))):
        raise RuntimeError('Stale timeframe data')
    frames=[calculate_timeframe_indicators(x[-n:]) for x,n in zip(candles,(100,200,300))]
    previous_rv=realized_volatility(candles[2],cutoff-3600)
    snapshot=make_multitimeframe_snapshot(*frames).replace(
        'Direction of the next completed 1-hour candle.',
        'Realized volatility class of the next completed 1-hour candle.')
    snapshot+='\n\n'+make_microstructure_snapshot(datetime.fromtimestamp(cutoff,timezone.utc))
    prompt=instructions(config)
    reason=''; confidence=None
    if predictor=='openai':
        from openai import OpenAI
        model=source.MODEL
        response=OpenAI().responses.create(model=model,input=prompt+
            '\nReturn JSON only: {"p_quiet":0.33,"p_normal":0.34,"p_active":0.33,"reason":"short explanation"}\nMARKET DATA:\n'+snapshot)
        text=response.output_text.strip()
        if text.startswith('```'):
            text='\n'.join(text.splitlines()[1:-1])
        result=json.loads(text)
        p=probabilities([result['p_'+label.lower()] for label in CLASSES])
        reason=str(result.get('reason',''))
    else:
        import os
        from typesafe_sdk import TypeSafeClient, Choice
        response=TypeSafeClient(api_key=os.environ['TYPESAFE_API_KEY'],timeout=120.0).system_one(
            model='jev-latest',state=snapshot,
            questions={'volatility':Choice(instructions=prompt,criteria={
                'QUIET':f"RV < {config['quiet_upper']:.17g}%",
                'NORMAL':f"{config['quiet_upper']:.17g}% <= RV < {config['active_lower']:.17g}%",
                'ACTIVE':f"RV >= {config['active_lower']:.17g}%"})})
        answer=response.answers['volatility']
        p=probabilities([answer.probabilities[label] for label in CLASSES])
        model=response.model; confidence=float(answer.confidence)
        reason=f'Jev choice={answer.choice}'
    save_prediction(cutoff,predictor,model,config,p,snapshot,previous_rv,classify(previous_rv,config),reason,confidence)
    print('Saved Phase 4',predictor,dict(zip(CLASSES,p)))
