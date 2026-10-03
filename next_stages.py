"""Additive, restart-safe 4R / 5 orchestration. Never writes Phase 4 rows."""
import fcntl
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from phase4 import DB_PATH, ROOT, now
from volatility import CLASSES, probabilities, winner, realized_volatility

REPEATS = 10
LIMIT = 96

@contextmanager
def database():
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        yield c

def migrate():
    with database() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS experiment_transitions (
          stage TEXT PRIMARY KEY, created_at TEXT NOT NULL, boundary INTEGER NOT NULL,
          design TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS phase4r_sources (
          source_id INTEGER PRIMARY KEY, source_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS phase4r_runs (
          source_id INTEGER NOT NULL, repeat_no INTEGER NOT NULL CHECK(repeat_no BETWEEN 1 AND 10),
          predictor TEXT NOT NULL, target_candle_time INTEGER NOT NULL,
          status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
          p_quiet REAL, p_normal REAL, p_active REAL, argmax_class TEXT,
          model_version TEXT, raw_json TEXT, error TEXT,
          PRIMARY KEY(source_id,repeat_no));
        CREATE TABLE IF NOT EXISTS phase5_predictions (
          id INTEGER PRIMARY KEY, target_candle_time INTEGER UNIQUE NOT NULL,
          started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
          context TEXT NOT NULL, previous_rv REAL NOT NULL,
          predicted_rv REAL, model_version TEXT, raw_json TEXT, error TEXT,
          actual_rv REAL, evaluated_at TEXT);
        ''')

def transition(c, stage, boundary, design):
    c.execute('INSERT OR IGNORE INTO experiment_transitions VALUES (?,?,?,?)',
              (stage, now(), boundary, json.dumps(design, sort_keys=True)))

def advance(clock=None):
    clock = time.time() if clock is None else clock
    with database() as c:
        c.execute('BEGIN IMMEDIATE')
        state = c.execute("SELECT * FROM experiment_transitions WHERE stage='4r'").fetchone()
        if state is None:
            gpt = c.execute("SELECT * FROM volatility_predictions WHERE predictor='openai' ORDER BY target_candle_time,id").fetchall()
            if len(gpt) < LIMIT:
                return '4'
            if len(gpt) != LIMIT:
                raise RuntimeError('Unexpected GPT Phase 4 count')
            boundary = gpt[-1]['target_candle_time']
            # Wait until target hour closes: both predictors get their final live cycle.
            if clock < boundary + 3600:
                return '4'
            sources = c.execute("SELECT p.*,cfg.config_json FROM volatility_predictions p JOIN phase4_configs cfg USING(config_id) WHERE predictor IN ('openai','jev') AND target_candle_time<=? ORDER BY target_candle_time,id", (boundary,)).fetchall()
            for row in sources:
                c.execute('INSERT INTO phase4r_sources VALUES (?,?)', (row['id'], json.dumps(dict(row))))
            transition(c, '4r', boundary, {'repeats': REPEATS, 'phase5_limit': LIMIT, 'missing': 'not imputed', 'phase4_boundary': '96th GPT target + completed hour', 'rv': '100*sqrt(sum(12 consecutive 5m log returns squared)); preceding close included', 'sd': 'sample', 'agreement': 'modal argmax frequency', 'paired': 'timestamp intersection'})
        n = c.execute('SELECT count(*) FROM phase4r_sources').fetchone()[0]
        done = c.execute("SELECT count(*) FROM phase4r_runs WHERE status='complete'").fetchone()[0]
        if done != n * REPEATS:
            return '4r'
        transition(c, '5', (int(clock)//3600+1)*3600, {'limit': LIMIT, 'predictor': 'openai', 'baseline': 'previous-hour RV', 'rv': 'identical to Phase 4', 'validation': 'finite non-negative numeric; no upper bound or clipping', 'stop': '96 successful live predictions; no optional stopping'})
        return '5'

def class_call(source):
    from phase4_runner import instructions
    config = json.loads(source['config_json'])
    prompt = instructions(config)
    if source['predictor'] == 'openai':
        from openai import OpenAI
        response = OpenAI(max_retries=0).responses.create(model=source['model_version'], input=prompt+
            '\nReturn JSON only: {"p_quiet":0.33,"p_normal":0.34,"p_active":0.33,"reason":"short explanation"}\nMARKET DATA:\n'+source['context'])
        raw = response.output_text.strip()
        if raw.startswith('```'):
            raw = '\n'.join(raw.splitlines()[1:-1])
        result = json.loads(raw)
        p = probabilities([result['p_'+label.lower()] for label in CLASSES])
        return p, getattr(response, 'model', source['model_version']), response.model_dump_json()
    import os
    from typesafe_sdk import TypeSafeClient, Choice, RetryPolicy
    response = TypeSafeClient(api_key=os.environ['TYPESAFE_API_KEY'], timeout=120.0, retry=RetryPolicy(max_retries=0)).system_one(
        model=source['model_version'], state=source['context'], questions={'volatility': Choice(
            instructions=prompt, criteria={
                'QUIET': f"RV < {config['quiet_upper']:.17g}%",
                'NORMAL': f"{config['quiet_upper']:.17g}% <= RV < {config['active_lower']:.17g}%",
                'ACTIVE': f"RV >= {config['active_lower']:.17g}%"})})
    p = probabilities([response.answers['volatility'].probabilities[label] for label in CLASSES])
    return p, response.model, response.model_dump_json()

def run_repeats(call=class_call, budget=20):
    with database() as c:
        sources = [json.loads(r[0]) for r in c.execute('SELECT source_json FROM phase4r_sources ORDER BY source_id')]
    count = 0
    for source in sources:
        for repeat in range(1, REPEATS+1):
            with database() as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT status FROM phase4r_runs WHERE source_id=? AND repeat_no=?', (source['id'], repeat)).fetchone()
                if row:
                    if row[0] != 'complete':
                        raise RuntimeError('Uncertain/failed API attempt requires reconciliation; see NEXT_STAGES.md')
                    continue
                if count >= budget:
                    return
                c.execute('INSERT INTO phase4r_runs(source_id,repeat_no,predictor,target_candle_time,status,started_at) VALUES (?,?,?,?,?,?)', (source['id'], repeat, source['predictor'], source['target_candle_time'], 'started', now()))
            try:
                p, model, raw = call(source)
                p = probabilities(p)
                with database() as c:
                    c.execute("UPDATE phase4r_runs SET status='complete',completed_at=?,p_quiet=?,p_normal=?,p_active=?,argmax_class=?,model_version=?,raw_json=? WHERE source_id=? AND repeat_no=?", (now(), *p, winner(p), model, raw, source['id'], repeat))
            except Exception as exc:
                with database() as c:
                    c.execute("UPDATE phase4r_runs SET status='failed',error=? WHERE source_id=? AND repeat_no=?", (str(exc), source['id'], repeat))
                raise
            count += 1

def validate_rv(value):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < 0:
        raise ValueError('predicted_rv must be a finite non-negative JSON number')
    return float(value)

def regression_call(snapshot):
    from openai import OpenAI
    from predict_gpt import MODEL
    prompt = ('Predict next completed 1h BTC-USD realized volatility using ONLY the supplied '
              '1h/15m/5m technicals, order book, aggressive trade flow. All observations precede the target hour. '
              'Do not use news, external knowledge or future data. Predict magnitude, not direction. '
              'RV = 100 * sqrt(sum of 12 squared consecutive 5m close-to-close log returns), '
              'including the return from the preceding 5m close to the first target close; non-annualized percent. '
              'Order book imbalance 0.5 is balanced; buy_ratio above 0.5 indicates aggressive buyers; '
              'microprice offset is microprice minus midpoint. Return JSON only: {"predicted_rv":0.25}. '
              'The value must be non-negative, with no upper bound.\nMARKET DATA:\n')
    response = OpenAI(max_retries=0).responses.create(model=MODEL, input=prompt+snapshot)
    return validate_rv(json.loads(response.output_text)['predicted_rv']), getattr(response,'model',MODEL), response.model_dump_json()

def run_regression(cutoff, clock=None, call=regression_call, builder=None):
    from phase4_runner import build_snapshot
    builder = builder or build_snapshot
    clock = time.time() if clock is None else clock
    with database() as c:
        state = c.execute("SELECT boundary FROM experiment_transitions WHERE stage='5'").fetchone()
        if state is None or cutoff < state[0] or not 0 <= clock-cutoff <= 600:
            return
        if c.execute("SELECT 1 FROM phase5_predictions WHERE status='started'").fetchone():
            raise RuntimeError('Phase 5 uncertain/failed attempt requires reconciliation')
        if c.execute("SELECT count(*) FROM phase5_predictions WHERE status='complete'").fetchone()[0] >= LIMIT:
            return
        if c.execute('SELECT 1 FROM phase5_predictions WHERE target_candle_time=?', (cutoff,)).fetchone():
            return
    snapshot, previous = builder(cutoff)
    previous = validate_rv(previous)
    with database() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT count(*) FROM phase5_predictions WHERE status='complete'").fetchone()[0] >= LIMIT:
            return
        c.execute("INSERT INTO phase5_predictions(target_candle_time,started_at,status,context,previous_rv) VALUES (?,?,'started',?,?)", (cutoff,now(),snapshot,previous))
    try:
        rv, model, raw = call(snapshot)
        rv = validate_rv(rv)
        with database() as c:
            c.execute("UPDATE phase5_predictions SET status='complete',completed_at=?,predicted_rv=?,model_version=?,raw_json=? WHERE target_candle_time=?", (now(),rv,model,raw,cutoff))
    except Exception as exc:
        with database() as c:
            c.execute("UPDATE phase5_predictions SET status='failed',error=? WHERE target_candle_time=?", (str(exc),cutoff))
        raise

def evaluate_regression(fetch=None, clock=None):
    from prepare_phase4 import fetch_candles
    fetch = fetch or fetch_candles
    clock = time.time() if clock is None else clock
    with database() as c:
        rows = c.execute("SELECT * FROM phase5_predictions WHERE status='complete' AND evaluated_at IS NULL").fetchall()
    for row in rows:
        t = row['target_candle_time']
        if clock < t+3600:
            continue
        try:
            rv = realized_volatility(fetch(t-300,t+3600),t)
        except Exception as exc:
            print('Phase 5 evaluation pending:', t, exc)
            continue
        with database() as c:
            c.execute('UPDATE phase5_predictions SET actual_rv=?,evaluated_at=? WHERE id=? AND evaluated_at IS NULL', (rv,now(),row['id']))

def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    # One scheduler owns transitions and model calls across processes/restarts.
    with open(ROOT/'.next-stages.lock','a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        migrate()
        stage = advance()
        import sys
        if '--repeats-only' in sys.argv:
            if stage == '4r':
                run_repeats()
                advance()
            return
        if stage == '4':
            from phase4_runner import run
            failures = []
            for predictor in ('openai','jev'):
                try:
                    run(predictor)
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise RuntimeError(str(failures))
        elif stage == '4r':
            run_repeats()
            advance()
        else:
            from predict_gpt import get_prediction_cutoff
            evaluate_regression()
            run_regression(get_prediction_cutoff())

if __name__ == '__main__':
    main()
