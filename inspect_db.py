#!/usr/bin/env python3

import sqlite3
from pathlib import Path
from datetime import datetime, timezone


DB_PATH = Path(__file__).parent / "btc.db"


def format_time(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M UTC")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            id,
            candle_time,
            model,
            predictor,
            model_version,
            p_up,
            p_down,
            confidence,
            actual_direction,
            actual_return,
            context
        FROM predictions
        ORDER BY candle_time DESC, predictor
        LIMIT 12
    """).fetchall()

    conn.close()

    for row in rows:
        print("=" * 70)

        print(f"ID:        {row['id']}")
        print(f"Time:      {format_time(row['candle_time'])}")
        print(f"Predictor: {row['predictor']}")
        print(f"Model:     {row['model_version'] or row['model']}")

        print()
        print(
            f"P(UP):     {row['p_up']:.3f}    "
            f"P(DOWN): {row['p_down']:.3f}"
        )

        if row["confidence"] is not None:
            print(
                f"Confidence: {row['confidence']:.3f}"
            )

        if row["actual_direction"] is not None:
            print(
                f"Actual:    {row['actual_direction']} "
                f"{row['actual_return']:+.3f}%"
            )

        print()
        print("--- CONTEXT ---")
        print(row["context"])
        print()


if __name__ == "__main__":
    main()
