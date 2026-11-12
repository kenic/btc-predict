"""Explicit one-shot commands; no imports with network or experiment side effects."""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from .core import KEYS, timestamp, validate_config
from .runtime import run
from .evaluation import evaluate, report


def main(argv=None):
    os.umask(0o077)
    p = argparse.ArgumentParser(description='Phase 8a: prospective BTC news comparison, paper only')
    sub = p.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check')
    check.add_argument('--config', default='phase8a.config.example.json')
    predict = sub.add_parser('predict')
    predict.add_argument('--db', required=True)
    predict.add_argument('--news-db', required=True)
    predict.add_argument('--config', default='phase8a.config.example.json')
    predict.add_argument('--as-of')
    predict.add_argument('--live', action='store_true')
    tick = sub.add_parser('tick')
    tick.add_argument('--db', required=True)
    tick.add_argument('--news-db', required=True)
    tick.add_argument('--candles-db', required=True)
    tick.add_argument('--config', default='phase8a.config.example.json')
    tick.add_argument('--live', action='store_true')
    score = sub.add_parser('evaluate')
    score.add_argument('--db', required=True)
    score.add_argument('--candles-db', required=True)
    summary = sub.add_parser('report')
    summary.add_argument('--db', required=True)
    demo = sub.add_parser('demo')
    demo.add_argument('--directory', required=True)
    args = p.parse_args(argv)
    try:
        if args.command in ('check', 'predict', 'tick'):
            config = validate_config(json.loads(Path(args.config).read_text()))
        if args.command == 'check':
            output = {'config_valid': True, 'live_enabled': config['live_enabled'],
                      'credentials_present': {e: bool(os.environ.get(k)) for e, k in KEYS.items()},
                      'account_verified': {e: m['account_verified'] for e, m in config['engines'].items()},
                      'pricing_verified': {e: m['pricing_verified'] for e, m in config['engines'].items()}}
        elif args.command == 'predict':
            cutoff = timestamp(args.as_of) if args.as_of else None
            output = run(args.db, args.news_db, config, cutoff, 'live' if args.live else 'offline')
        elif args.command == 'tick':
            from . import store
            store.initialize(args.db, config)
            now = time.time()
            output = {'prediction': {'status': 'outside_start_window'}}
            if now % config['interval_seconds'] <= config['max_start_delay_seconds']:
                output['prediction'] = run(args.db, args.news_db, config, mode='live' if args.live else 'offline')
            output['evaluation'] = evaluate(args.db, args.candles_db)
        elif args.command == 'evaluate':
            output = evaluate(args.db, args.candles_db)
        elif args.command == 'report':
            output = report(args.db)
        else:
            from .demo import build_demo
            output = build_demo(args.directory)
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except Exception:
        # No traceback or exception string: provider/SQLite errors can contain secrets or source text.
        print(json.dumps({'status': 'error', 'error': 'command_failed_check_config_paths_and_contract'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
