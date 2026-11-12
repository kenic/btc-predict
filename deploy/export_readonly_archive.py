"""Export a consistent SQLite reader image from inside the authorized collector unit.

No fetching, inference or changes to the source DB. SQLite backup includes committed
WAL data; a raw copy or immutable=1 against the live DB would not be safe.
"""
import argparse
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def export(source, destination, kind):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or kind not in ('news', 'candles'):
        raise ValueError('invalid_archive_paths_or_kind')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=destination.name+'.export-', dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=5)) as src:
            src.execute('PRAGMA query_only=ON')
            tables={r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required={'feeds','versions','runs'} if kind=='news' else {'candles_5m','collector_state'}
            if not required <= tables:raise ValueError('wrong_archive_kind')
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
                dst.commit()
                if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0]!='delete':
                    raise ValueError('reader_journal_mode')
                if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('archive_integrity')
        with temporary.open('rb') as f:os.fsync(f.fileno())
        temporary.chmod(0o440)
        temporary.replace(destination)
        fd=os.open(destination.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return {'status':'exported','kind':kind}
    finally:
        temporary.unlink(missing_ok=True)


def main():
    import json
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True)
    p.add_argument('--destination',required=True)
    p.add_argument('--kind',choices=('news','candles'),required=True)
    a=p.parse_args()
    try:
        print(json.dumps(export(a.source,a.destination,a.kind)));return 0
    except Exception:
        print(json.dumps({'status':'archive_export_failed','kind':a.kind}));return 1


if __name__=='__main__':raise SystemExit(main())
