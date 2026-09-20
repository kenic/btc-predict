import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).parent / "btc.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute("""
            SELECT
                id,
                model,
                predictor,
                model_version,
                confidence,
                reason
            FROM predictions
            ORDER BY id
        """).fetchall()

        print("===== BEFORE =====")

        for row in rows:
            print(dict(row))

        #
        # Existing GPT record
        #
        conn.execute("""
            UPDATE predictions
            SET
                predictor = 'openai',
                model_version = model,
                confidence = NULL
            WHERE model = 'gpt-5.6-sol'
              AND predictor IS NULL
        """)

        #
        # Existing Jev record
        #
        # We know this prediction was produced by:
        #
        #   jev-1.13.0
        #   confidence = 0.660
        #
        conn.execute("""
            UPDATE predictions
            SET
                predictor = 'jev',
                model_version = 'jev-1.13.0',
                confidence = 0.660
            WHERE model = 'jev'
              AND predictor IS NULL
        """)

        conn.commit()

        rows = conn.execute("""
            SELECT
                id,
                model,
                predictor,
                model_version,
                p_up,
                p_down,
                confidence
            FROM predictions
            ORDER BY id
        """).fetchall()

        print()
        print("===== AFTER =====")

        for row in rows:
            print(dict(row))

    finally:
        conn.close()


if __name__ == "__main__":
    main()
