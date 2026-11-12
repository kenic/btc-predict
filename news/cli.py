"""python -m news.cli {collect,snapshot,health}; JSON stdout, safe logs stderr."""
import argparse
import json
import logging
import math
import sqlite3
import sys
import time
from pathlib import Path

from . import store
from .collector import collect
from .feeds import load_config, timestamp


def instant(value):
    try:
        n = float(value)
        if not math.isfinite(n) or n < 0:
            raise ValueError()
        return n
    except ValueError:
        n = timestamp(value)
        if n is None or n < 0:
            raise argparse.ArgumentTypeError('Use UTC epoch or ISO-8601 with timezone')
        return n


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path(__file__).resolve().parent.parent/'news.db')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('collect')
    p.add_argument('--config', type=Path, required=True)
    p = sub.add_parser('snapshot')
    p.add_argument('--as-of', type=instant, required=True)
    p.add_argument('--hours', type=int, default=24)
    p.add_argument('--keep-duplicates', action='store_true')
    p = sub.add_parser('health')
    p.add_argument('--as-of', type=instant)
    p.add_argument('--format', choices=('json', 'text'), default='json')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    try:
        if args.command == 'collect':
            result = collect(args.db, load_config(args.config))
            code = int(any(r['status'] in ('failed', 'deferred') or r['rejected'] for r in result))
        elif args.command == 'snapshot':
            if args.as_of > time.time():
                raise ValueError('future_asof_not_supported')
            result = store.snapshot(args.db, args.as_of, args.hours, not args.keep_duplicates)
            code = 0
        else:
            result = store.health(args.db, args.as_of)
            code = int(any(r['enabled'] and (r['stale'] or r['latest_rejected'] or r['latest_status'] in ('failed','deferred')) for r in result))
            if args.format == 'text':
                print('FEED                 ENABLED  STATUS         STALE   GAPS  REJECTED  DUPLICATES')
                for r in result:
                    print('{:20} {:7}  {:14} {:7} {:5} {:9} {:9.1%}'.format(
                        r['feed_id'], str(r['enabled']), r['latest_status'], str(r['stale']),
                        len(r['gaps']), r['rejected'], r['duplicate_rate']))
                return code
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        return code
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        # Do not print exception messages, URLs, headers, response text or local secrets.
        logging.error('{"status":"failed","error":"configuration_storage_or_lock_error"}')
        return 2


if __name__ == '__main__':
    sys.exit(main())
