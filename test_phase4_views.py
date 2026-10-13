"""Dashboard regressions using isolated SQLite fixtures, never the live DB."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import phase4
import phase4_views as views


class CohortViewsTests(unittest.TestCase):
    def test_uneven_pending_cohorts_and_unmigrated_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'btc.db'
            with patch.object(phase4, 'DB_PATH', db):
                phase4.init_schema()
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE predictions(predictor TEXT, phase TEXT, evaluated_at TEXT)')
                config = dict(quiet_upper=.2, active_lower=.4,
                              historical_hours=336, majority_class='QUIET')
                connection.execute('INSERT INTO phase4_configs VALUES (?,?)', ('fixture', json.dumps(config)))
                # Insertion order differs from target order; Jev misses hours.
                for model, count, spacing in [('openai', 52, 1), ('jev', 49, 2)]:
                    for number in range(count, 0, -1):
                        actual = None if number == 48 or number == count else ('QUIET' if number < 48 else 'ACTIVE')
                        connection.execute('''INSERT INTO volatility_predictions
                            (created_at,target_candle_time,predictor,model_version,config_id,
                             p_quiet,p_normal,p_active,context,previous_rv,persistence_class,
                             actual_class,evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            ('fixture', number * spacing * 3600, model, 'fixture', 'fixture',
                             1., 0., 0., 'fixture', .1, 'QUIET', actual, 'done' if actual else None))
                before = connection.execute('SELECT * FROM volatility_predictions ORDER BY id').fetchall()
                legacy = connection.execute('SELECT * FROM predictions').fetchall()
            with patch.object(views, 'DB_PATH', db):
                client = app.app.test_client()
                pages = {}
                for url in ('/', '/analyze/?phase=phase4'):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    rendered = response.get_data(as_text=True)
                    pages[url] = rendered
                    self.assertIn('<td>openai</td><td>48</td><td>1</td><td>47</td><td>100.0%</td><td>0.0000</td>', rendered)
                    self.assertIn('<td>openai</td><td>4</td><td>1</td><td>3</td><td>0.0%</td><td>2.0000</td>', rendered)
                    self.assertIn('<td>openai</td><td>52</td><td>2</td><td>50</td><td>94.0%</td><td>0.1200</td>', rendered)
                    self.assertIn('<td>jev</td><td>1</td><td>1</td><td>0</td><td>—</td><td>—</td>', rendered)
                analysis = pages['/analyze/?phase=phase4']
                sections = {s: analysis.split(f'<section id="{s}"', 1)[1].split('</section>', 1)[0]
                            for s, _ in views.SEGMENTS}
                for section in sections.values():
                    self.assertEqual(section.count('confusion matrix'), 2)
                    self.assertEqual(section.count('class calibration'), 2)
                self.assertIn('<td>QUIET</td><td>47</td><td>0</td><td>0</td>', sections['phase4a'])
                self.assertIn('<td>ACTIVE</td><td>3</td><td>0</td><td>0</td>', sections['phase4b'])
                self.assertIn('<td>QUIET</td><td>90–100%</td><td>3</td><td>100.0%</td><td>0.0%</td>', sections['phase4b'])
                with sqlite3.connect(db) as connection:
                    self.assertEqual(before, connection.execute('SELECT * FROM volatility_predictions ORDER BY id').fetchall())
                    self.assertEqual(legacy, connection.execute('SELECT * FROM predictions').fetchall())
                    connection.execute('DROP VIEW phase4_segments')
                # Fallback must produce exactly the same metrics and history.
                for url, rendered in pages.items():
                    self.assertEqual(client.get(url).get_data(as_text=True), rendered)

    def test_empty_cohorts(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'btc.db'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE predictions(predictor TEXT, phase TEXT, evaluated_at TEXT)')
            with patch.object(views, 'DB_PATH', db):
                rendered = views.page(analysis=True)
            self.assertEqual(rendered.count('<td>0</td><td>0</td><td>0</td><td>—</td><td>—</td>'), 6)
            for segment, _ in views.SEGMENTS:
                self.assertIn(f'<section id="{segment}"', rendered)


if __name__ == '__main__':
    unittest.main()
