#!/usr/bin/env python3

import sqlite3
from pathlib import Path

DB = Path(__file__).with_name("btc.db")


def main():
    conn = sqlite3.connect(DB)

    try:
        # phase列が存在するか確認
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(predictions)")
        }

        if "phase" not in columns:
            conn.execute(
                "ALTER TABLE predictions ADD COLUMN phase TEXT"
            )
            print("Added column: phase")
        else:
            print("Column phase already exists")

        # 既存データをPhase 1にする
        cur = conn.execute("""
            UPDATE predictions
            SET phase = 'phase1'
            WHERE phase IS NULL
        """)

        print(f"Updated {cur.rowcount} rows to phase1")

        conn.commit()

        # 確認
        print()
        print("Current predictions:")

        rows = conn.execute("""
            SELECT
                phase,
                model,
                COUNT(*) AS n
            FROM predictions
            GROUP BY phase, model
            ORDER BY phase, model
        """)

        for phase, model, n in rows:
            print(f"{phase:10} {model:10} {n:4}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
