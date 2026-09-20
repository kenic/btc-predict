import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).parent / "btc.db"


def main():
    conn = sqlite3.connect(DB_PATH)

    try:
        rows = conn.execute("""
            SELECT
                id,
                candle_close,
                actual_close
            FROM predictions
            WHERE actual_close IS NOT NULL
              AND actual_return IS NULL
        """).fetchall()

        print(
            f"Records to update: {len(rows)}"
        )

        for row in rows:
            prediction_id = row[0]
            old_close = float(row[1])
            actual_close = float(row[2])

            actual_return = (
                (actual_close - old_close)
                / old_close
                * 100
            )

            conn.execute("""
                UPDATE predictions
                SET actual_return = ?
                WHERE id = ?
            """, (
                actual_return,
                prediction_id,
            ))

            print(
                f"id={prediction_id}: "
                f"{actual_return:+.3f}%"
            )

        conn.commit()

    finally:
        conn.close()


if __name__ == "__main__":
    main()
