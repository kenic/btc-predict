import sqlite3
import tempfile
import unittest
from pathlib import Path
from deploy.export_readonly_archive import export
from news import store


class ReadonlyExportTests(unittest.TestCase):
    def test_live_wal_included_and_source_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source.db';target=Path(d)/'reader.db'
            store.initialize(source)
            with sqlite3.connect(source) as writer:
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute("INSERT INTO feeds VALUES('test',1)");writer.commit()
                self.assertTrue(Path(str(source)+'-wal').exists())
                self.assertEqual(export(source,target,'news')['status'],'exported')
                reader=sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)
                self.assertEqual(reader.execute('PRAGMA journal_mode').fetchone()[0],'delete')
                self.assertEqual(reader.execute('SELECT count(*) FROM feeds').fetchone()[0],1)
                with self.assertRaises(sqlite3.OperationalError):reader.execute('DELETE FROM feeds')
                reader.close()
                self.assertEqual(writer.execute('PRAGMA journal_mode').fetchone()[0],'wal')
                self.assertEqual(writer.execute('SELECT count(*) FROM feeds').fetchone()[0],1)
            self.assertFalse(Path(str(target)+'-wal').exists())
            self.assertFalse(Path(str(target)+'-shm').exists())

    def test_wrong_source_preserves_prior_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source.db';target=Path(d)/'reader.db'
            store.initialize(source);export(source,target,'news');before=target.read_bytes()
            with self.assertRaises(ValueError):export(source,target,'candles')
            self.assertEqual(target.read_bytes(),before)
            with self.assertRaises(ValueError):export(source,source,'news')

    def test_atomic_replacement_preserves_open_reader(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source.db';target=Path(d)/'reader.db'
            store.initialize(source);export(source,target,'news')
            reader=sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)
            reader.execute('BEGIN');self.assertEqual(reader.execute('SELECT count(*) FROM feeds').fetchone()[0],0)
            with sqlite3.connect(source) as c:c.execute("INSERT INTO feeds VALUES('test',1)")
            export(source,target,'news')
            self.assertEqual(reader.execute('SELECT count(*) FROM feeds').fetchone()[0],0)
            with sqlite3.connect(target.as_uri()+'?mode=ro',uri=True) as new:
                self.assertEqual(new.execute('SELECT count(*) FROM feeds').fetchone()[0],1)
            reader.close()
