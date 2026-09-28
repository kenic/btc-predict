#!/usr/bin/env python3

import sqlite3
import time

from datetime import datetime, timezone
from pathlib import Path

import requests


DB_PATH = Path(__file__).parent / "market.db"

URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/book"
)

HEADERS = {
    "User-Agent": "btc-predict-orderbook/0.1"
}

SAMPLE_INTERVAL = 10


def connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS
            orderbook_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                timestamp INTEGER NOT NULL,
                created_at TEXT NOT NULL,

                best_bid REAL NOT NULL,
                best_ask REAL NOT NULL,

                midpoint REAL NOT NULL,
                spread REAL NOT NULL,

                top5_bid_volume REAL NOT NULL,
                top5_ask_volume REAL NOT NULL,
                top5_imbalance REAL NOT NULL,

                top10_bid_volume REAL NOT NULL,
                top10_ask_volume REAL NOT NULL,
                top10_imbalance REAL NOT NULL,

                top20_bid_volume REAL NOT NULL,
                top20_ask_volume REAL NOT NULL,
                top20_imbalance REAL NOT NULL,

                microprice REAL NOT NULL,
                microprice_offset REAL NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_orderbook_timestamp
            ON orderbook_samples(timestamp)
        """)

        conn.commit()


def fetch_orderbook():
    r = requests.get(
        URL,
        params={
            "level": 2,
        },
        headers=HEADERS,
        timeout=10,
    )

    r.raise_for_status()

    return r.json()


def volume_sum(levels, n):
    return sum(
        float(level[1])
        for level in levels[:n]
    )


def imbalance(
    bid_volume,
    ask_volume,
):
    total = (
        bid_volume + ask_volume
    )

    if total == 0:
        return 0.5

    return (
        bid_volume / total
    )


def calculate_sample(book):
    bids = book["bids"]
    asks = book["asks"]

    if len(bids) < 20:
        raise RuntimeError(
            "Order book has fewer than "
            "20 bid levels"
        )

    if len(asks) < 20:
        raise RuntimeError(
            "Order book has fewer than "
            "20 ask levels"
        )

    best_bid = float(
        bids[0][0]
    )

    best_ask = float(
        asks[0][0]
    )

    best_bid_size = float(
        bids[0][1]
    )

    best_ask_size = float(
        asks[0][1]
    )

    midpoint = (
        best_bid + best_ask
    ) / 2.0

    spread = (
        best_ask - best_bid
    )

    #
    # Depth / imbalance
    #

    top5_bid = volume_sum(
        bids,
        5,
    )

    top5_ask = volume_sum(
        asks,
        5,
    )

    top10_bid = volume_sum(
        bids,
        10,
    )

    top10_ask = volume_sum(
        asks,
        10,
    )

    top20_bid = volume_sum(
        bids,
        20,
    )

    top20_ask = volume_sum(
        asks,
        20,
    )

    top5_imbalance = imbalance(
        top5_bid,
        top5_ask,
    )

    top10_imbalance = imbalance(
        top10_bid,
        top10_ask,
    )

    top20_imbalance = imbalance(
        top20_bid,
        top20_ask,
    )

    #
    # Microprice
    #
    # If bid size is large relative to ask size,
    # microprice moves toward the ask.
    #

    best_total = (
        best_bid_size
        + best_ask_size
    )

    if best_total == 0:
        microprice = midpoint

    else:
        microprice = (
            best_ask * best_bid_size
            + best_bid * best_ask_size
        ) / best_total

    microprice_offset = (
        microprice - midpoint
    )

    now = datetime.now(
        timezone.utc
    )

    return {
        "timestamp": int(
            now.timestamp()
        ),

        "created_at": (
            now.isoformat()
        ),

        "best_bid": best_bid,
        "best_ask": best_ask,

        "midpoint": midpoint,
        "spread": spread,

        "top5_bid_volume": top5_bid,
        "top5_ask_volume": top5_ask,
        "top5_imbalance": (
            top5_imbalance
        ),

        "top10_bid_volume": top10_bid,
        "top10_ask_volume": top10_ask,
        "top10_imbalance": (
            top10_imbalance
        ),

        "top20_bid_volume": top20_bid,
        "top20_ask_volume": top20_ask,
        "top20_imbalance": (
            top20_imbalance
        ),

        "microprice": microprice,
        "microprice_offset": (
            microprice_offset
        ),
    }


def save_sample(sample):
    with connect() as conn:
        cursor = conn.execute("""
            INSERT INTO orderbook_samples (
                timestamp,
                created_at,

                best_bid,
                best_ask,

                midpoint,
                spread,

                top5_bid_volume,
                top5_ask_volume,
                top5_imbalance,

                top10_bid_volume,
                top10_ask_volume,
                top10_imbalance,

                top20_bid_volume,
                top20_ask_volume,
                top20_imbalance,

                microprice,
                microprice_offset
            )
            VALUES (
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?
            )
        """, (
            sample["timestamp"],
            sample["created_at"],

            sample["best_bid"],
            sample["best_ask"],

            sample["midpoint"],
            sample["spread"],

            sample["top5_bid_volume"],
            sample["top5_ask_volume"],
            sample["top5_imbalance"],

            sample["top10_bid_volume"],
            sample["top10_ask_volume"],
            sample["top10_imbalance"],

            sample["top20_bid_volume"],
            sample["top20_ask_volume"],
            sample["top20_imbalance"],

            sample["microprice"],
            sample["microprice_offset"],
        ))

        conn.commit()

        return cursor.lastrowid


def print_sample(sample):
    print(
        f"{sample['created_at']} "
        f"bid={sample['best_bid']:.2f} "
        f"ask={sample['best_ask']:.2f} "
        f"spread={sample['spread']:.2f}"
    )

    print(
        "  imbalance "
        f"5={sample['top5_imbalance']:.3f} "
        f"10={sample['top10_imbalance']:.3f} "
        f"20={sample['top20_imbalance']:.3f}"
    )

    print(
        "  microprice="
        f"{sample['microprice']:.2f} "
        "offset="
        f"{sample['microprice_offset']:+.4f}"
    )


def collect_once():
    book = fetch_orderbook()

    sample = calculate_sample(
        book
    )

    sample_id = save_sample(
        sample
    )

    print_sample(
        sample
    )

    print(
        f"Saved sample id={sample_id}"
    )


def collect_forever():
    print(
        "Starting order book collector"
    )

    print(
        f"Interval: {SAMPLE_INTERVAL}s"
    )

    while True:
        started = time.monotonic()

        try:
            book = fetch_orderbook()

            sample = calculate_sample(
                book
            )

            save_sample(
                sample
            )

            print_sample(
                sample
            )

        except Exception as exc:
            print(
                f"ERROR: {exc}",
                flush=True,
            )

        elapsed = (
            time.monotonic()
            - started
        )

        sleep_time = max(
            0,
            SAMPLE_INTERVAL - elapsed,
        )

        time.sleep(
            sleep_time
        )


def main():
    init_db()

    #
    # For initial testing, collect one
    # sample and exit.
    #
    collect_forever()


if __name__ == "__main__":
    main()
