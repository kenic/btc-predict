#!/usr/bin/env python3

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


DB_PATH = Path(__file__).parent / "market.db"

URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/trades"
)

HEADERS = {
    "User-Agent": "btc-predict-trades/0.1"
}

POLL_INTERVAL = 10


def connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id INTEGER PRIMARY KEY,

                timestamp REAL NOT NULL,
                trade_time TEXT NOT NULL,

                price REAL NOT NULL,
                size REAL NOT NULL,
                quote_volume REAL NOT NULL,

                maker_side TEXT NOT NULL,
                aggressor_side TEXT NOT NULL,

                collected_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_trades_timestamp
            ON trades(timestamp)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_trades_aggressor
            ON trades(aggressor_side, timestamp)
        """)

        conn.commit()


def fetch_trades():
    response = requests.get(
        URL,
        params={
            "limit": 1000,
        },
        headers=HEADERS,
        timeout=10,
    )

    response.raise_for_status()

    return response.json()


def parse_time(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    return datetime.fromisoformat(value)


def normalize_trade(trade):
    maker_side = trade["side"]

    #
    # IMPORTANT:
    #
    # Coinbase's "side" is the MAKER order side.
    #
    # maker sell -> aggressive buyer
    # maker buy  -> aggressive seller
    #

    if maker_side == "sell":
        aggressor_side = "buy"

    elif maker_side == "buy":
        aggressor_side = "sell"

    else:
        raise ValueError(
            f"Unknown side: {maker_side}"
        )

    dt = parse_time(
        trade["time"]
    )

    price = float(
        trade["price"]
    )

    size = float(
        trade["size"]
    )

    now = datetime.now(
        timezone.utc
    )

    return {
        "trade_id": int(
            trade["trade_id"]
        ),

        "timestamp": (
            dt.timestamp()
        ),

        "trade_time": (
            dt.isoformat()
        ),

        "price": price,
        "size": size,

        "quote_volume": (
            price * size
        ),

        "maker_side": (
            maker_side
        ),

        "aggressor_side": (
            aggressor_side
        ),

        "collected_at": (
            now.isoformat()
        ),
    }


def save_trades(trades):
    inserted = 0

    with connect() as conn:
        for raw_trade in trades:
            trade = normalize_trade(
                raw_trade
            )

            cursor = conn.execute("""
                INSERT OR IGNORE INTO trades (
                    trade_id,

                    timestamp,
                    trade_time,

                    price,
                    size,
                    quote_volume,

                    maker_side,
                    aggressor_side,

                    collected_at
                )
                VALUES (
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?
                )
            """, (
                trade["trade_id"],

                trade["timestamp"],
                trade["trade_time"],

                trade["price"],
                trade["size"],
                trade["quote_volume"],

                trade["maker_side"],
                trade["aggressor_side"],

                trade["collected_at"],
            ))

            inserted += (
                cursor.rowcount
            )

        conn.commit()

    return inserted


def print_summary(
    trades,
    inserted,
):
    if not trades:
        print(
            "No trades returned",
            flush=True,
        )
        return

    normalized = [
        normalize_trade(t)
        for t in trades
    ]

    newest = max(
        t["timestamp"]
        for t in normalized
    )

    oldest = min(
        t["timestamp"]
        for t in normalized
    )

    buy_volume = sum(
        t["size"]
        for t in normalized
        if t["aggressor_side"] == "buy"
    )

    sell_volume = sum(
        t["size"]
        for t in normalized
        if t["aggressor_side"] == "sell"
    )

    total_volume = (
        buy_volume
        + sell_volume
    )

    if total_volume:
        buy_ratio = (
            buy_volume
            / total_volume
        )
    else:
        buy_ratio = 0.5

    oldest_dt = datetime.fromtimestamp(
        oldest,
        timezone.utc,
    )

    newest_dt = datetime.fromtimestamp(
        newest,
        timezone.utc,
    )

    print(
        f"received={len(trades)} "
        f"inserted={inserted} "
        f"range="
        f"{oldest_dt.isoformat()} "
        f".. "
        f"{newest_dt.isoformat()}",
        flush=True,
    )

    print(
        f"  aggressive volume "
        f"buy={buy_volume:.6f} BTC "
        f"sell={sell_volume:.6f} BTC "
        f"buy_ratio={buy_ratio:.3f}",
        flush=True,
    )


def collect_once():
    trades = fetch_trades()

    inserted = save_trades(
        trades
    )

    print_summary(
        trades,
        inserted,
    )


def collect_forever():
    print(
        "Starting trade collector",
        flush=True,
    )

    print(
        f"Poll interval: "
        f"{POLL_INTERVAL}s",
        flush=True,
    )

    while True:
        started = time.monotonic()

        try:
            trades = fetch_trades()

            inserted = save_trades(
                trades
            )

            print_summary(
                trades,
                inserted,
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
            POLL_INTERVAL - elapsed,
        )

        time.sleep(
            sleep_time
        )


def main():
    init_db()

    # First test:
    #collect_once()

    # After testing, replace the line above with:
    collect_forever()


if __name__ == "__main__":
    main()
