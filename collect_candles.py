#!/usr/bin/env python3
"""One bounded, future-only market-data collection cycle, independent of experiments."""
import argparse
import json
import sys
import time
from pathlib import Path
from candle_store import DB_PATH, SECONDS, SETTLE_SECONDS, initialize, save, validate

RECOVERY_CANDLES = 6


def collect(path=DB_PATH, fetch=None, clock=None):
    clock = time.time() if clock is None else clock
    floor = initialize(path, clock)
    end = int((clock-SETTLE_SECONDS)//SECONDS)*SECONDS
    start = max(floor, end-RECOVERY_CANDLES*SECONDS)
    result = dict(start=start, end=end, inserted=0, duplicates=0, conflicts=0, invalid=0, missing=0)
    if start >= end:
        return dict(result, status='waiting for first future completed candle')
    if fetch is None:
        # Reuse Phase 4 provider utility, without running preparation/evaluation.
        from prepare_phase4 import fetch_candles
        fetch = fetch_candles
    raw = fetch(start, end)
    if not isinstance(raw, list):
        raise ValueError('Unexpected provider response')
    candles, bad = {}, set()
    for item in raw:
        try:
            candle = validate(item, start, end, clock)
        except (ValueError, TypeError, IndexError, OverflowError):
            result['invalid'] += 1
            # Invalid duplicates of an otherwise valid timestamp must not be saved.
            if isinstance(item, (list, tuple)) and item:
                t = item[0]
                if isinstance(t, (int, float)) and not isinstance(t, bool) and start <= t < end:
                    bad.add(t)
            continue
        t = candle[0]
        if t in candles and candles[t] != candle:
            bad.add(t)
            result['conflicts'] += 1
        candles[t] = candle
    rows = [c for t,c in sorted(candles.items()) if t not in bad]
    result['missing'] = (end-start)//SECONDS-len(rows)
    saved = save(path, rows, clock)
    result['conflicts'] += saved.pop('conflicts')
    result.update(saved, status='collected')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DB_PATH, help='Dedicated archive only; never btc.db/market.db')
    args = parser.parse_args()
    if args.db.name in ('btc.db', 'market.db'):
        parser.error('Use a dedicated candles.db, not an experiment/collector DB')
    try:
        print(json.dumps(collect(args.db)))
    except Exception as exc:
        print(f'5m candle collector failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
