"""Separate additive Phase 4 storage; legacy predictions are never repurposed."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from volatility import CLASSES, probabilities
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "btc.db"
CONFIG_PATH = ROOT / "phase4_config.json"

def now():
    return datetime.now(timezone.utc).isoformat()

def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def init_schema():
    with sqlite3.connect(DB_PATH) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS phase4_configs (
          config_id TEXT PRIMARY KEY, config_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS volatility_predictions (
          id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
          target_candle_time INTEGER NOT NULL, predictor TEXT NOT NULL,
          model_version TEXT NOT NULL, config_id TEXT NOT NULL,
          p_quiet REAL NOT NULL CHECK(p_quiet BETWEEN 0 AND 1),
          p_normal REAL NOT NULL CHECK(p_normal BETWEEN 0 AND 1),
          p_active REAL NOT NULL CHECK(p_active BETWEEN 0 AND 1),
          context TEXT NOT NULL, reason TEXT, confidence REAL,
          previous_rv REAL NOT NULL, persistence_class TEXT NOT NULL,
          actual_rv REAL, actual_class TEXT, correct INTEGER, brier REAL,
          evaluated_at TEXT,
          UNIQUE(target_candle_time,predictor),
          FOREIGN KEY(config_id) REFERENCES phase4_configs(config_id));
        CREATE TRIGGER IF NOT EXISTS freeze_phase4_config_update
        BEFORE UPDATE ON phase4_configs BEGIN SELECT RAISE(ABORT,'Frozen config'); END;
        CREATE TRIGGER IF NOT EXISTS freeze_phase4_config_delete
        BEFORE DELETE ON phase4_configs BEGIN SELECT RAISE(ABORT,'Frozen config'); END;
        """)

def assert_phase3_closed(c):
    rows = c.execute("SELECT predictor, count(*), sum(evaluated_at IS NOT NULL) FROM predictions WHERE phase='phase3' GROUP BY predictor").fetchall()
    if sorted(rows) != [('jev', 48, 48), ('openai', 48, 48)]:
        raise RuntimeError("Phase 3 must contain exactly 48 evaluated predictions for each model")
    pairs = c.execute("SELECT candle_time FROM predictions WHERE phase='phase3' GROUP BY candle_time HAVING count(*)=2 AND count(DISTINCT predictor)=2").fetchall()
    if len(pairs) != 48:
        raise RuntimeError("Phase 3 model timestamps do not match")

def load_config(require_enabled=False):
    config = json.loads(CONFIG_PATH.read_text())
    if not (0 < config['quiet_upper'] < config['active_lower']):
        raise RuntimeError('Invalid frozen thresholds')
    with sqlite3.connect(DB_PATH) as c:
        assert_phase3_closed(c)
        row = c.execute('SELECT config_json FROM phase4_configs WHERE config_id=?', (config_hash(config),)).fetchone()
        if row is None or json.loads(row[0]) != config:
            raise RuntimeError('Config differs from frozen database copy')
    if not (ROOT / 'backups/phase3-boundary/closed-btc.db').is_file():
        raise RuntimeError('Phase 3 closed backup missing')
    if require_enabled:
        import subprocess
        if subprocess.run(['git','rev-parse','--verify','phase3-complete'],cwd=ROOT,
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            raise RuntimeError('Phase 3 completion tag missing')
    if require_enabled and not (ROOT / 'phase4.enabled').is_file():
        raise RuntimeError('Phase 4 is prepared but disabled; see PHASE4.md')
    return config

def save_prediction(target, predictor, model, config, p, snapshot, previous_rv, persistence, reason='', confidence=None):
    p = probabilities(p)
    with sqlite3.connect(DB_PATH) as c:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('BEGIN IMMEDIATE')
        assert_phase3_closed(c)
        n = c.execute('SELECT count(*) FROM volatility_predictions WHERE predictor=?', (predictor,)).fetchone()[0]
        if n >= config['prediction_limit']:
            raise RuntimeError('Phase 4 prediction limit reached')
        c.execute("""INSERT INTO volatility_predictions
        (created_at,target_candle_time,predictor,model_version,config_id,p_quiet,p_normal,p_active,context,previous_rv,persistence_class,reason,confidence)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (now(), target,predictor,model,config_hash(config),*p,snapshot,previous_rv,persistence,reason,confidence))
