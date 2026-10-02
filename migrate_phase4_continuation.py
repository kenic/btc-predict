"""Back up live SQLite consistently, then add the read-only cohort view.

Run locally on the server between hourly runs. Never copies a stale DB back.
"""
import sqlite3
from datetime import datetime, timezone
from phase4 import ROOT, DB_PATH, config_hash, init_schema, load_config, prediction_limit


def migrate():
    config = load_config()
    prediction_limit(config)
    # Validate without creating a missing database.
    with sqlite3.connect(DB_PATH.resolve().as_uri() + '?mode=ro', uri=True) as source:
        if source.execute('SELECT 1 FROM volatility_predictions WHERE config_id != ? LIMIT 1',
                          (config_hash(config),)).fetchone():
            raise RuntimeError('Existing predictions use a different frozen config')
        counts = source.execute('SELECT predictor,count(*) FROM volatility_predictions GROUP BY predictor').fetchall()
        if any(model not in ('openai','jev') or n > 96 for model,n in counts):
            raise RuntimeError('Existing data outside the 48+48 design')
        folder = ROOT / 'backups' / ('phase4-continuation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        folder.mkdir(parents=True, exist_ok=False)
        with sqlite3.connect(folder / 'btc.db') as destination:
            source.backup(destination)
        (folder / 'phase4_config.json').write_bytes((ROOT / 'phase4_config.json').read_bytes())
    init_schema()
    with sqlite3.connect(DB_PATH) as connection:
        groups = connection.execute('SELECT predictor,cohort,count(*) FROM phase4_segments GROUP BY predictor,cohort').fetchall()
    print('Backup:', folder)
    print('Cohorts:', groups)
    print('Ready: 48 per cohort, 96 per model. Frozen config unchanged.')


if __name__ == '__main__':
    migrate()
