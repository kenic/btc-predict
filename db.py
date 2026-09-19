import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).parent / "btc.db"


def connect():
    return sqlite3.connect(DB_PATH)


def column_exists(conn, table, column):
    rows = conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return any(
        row[1] == column
        for row in rows
    )


def init_db():
    with connect() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                created_at TEXT NOT NULL,

                candle_time INTEGER NOT NULL,
                candle_close REAL NOT NULL,

                model TEXT NOT NULL,

                p_up REAL NOT NULL,
                p_down REAL NOT NULL,

                reason TEXT,
                context TEXT NOT NULL,

                target_candle_time INTEGER NOT NULL,

                actual_close REAL,
                actual_direction TEXT,
                correct INTEGER,

                evaluated_at TEXT,

                UNIQUE(candle_time, model)
            )
        """)

        #
        # Schema migration
        #

        if not column_exists(
            conn,
            "predictions",
            "actual_return",
        ):
            conn.execute("""
                ALTER TABLE predictions
                ADD COLUMN actual_return REAL
            """)


        if not column_exists(
            conn,
            "predictions",
            "predictor",
        ):
            conn.execute("""
                ALTER TABLE predictions
                ADD COLUMN predictor TEXT
            """)

        if not column_exists(
            conn,
            "predictions",
            "model_version",
        ):
            conn.execute("""
                ALTER TABLE predictions
                ADD COLUMN model_version TEXT
            """)

        if not column_exists(
            conn,
            "predictions",
            "confidence",
        ):
            conn.execute("""
                ALTER TABLE predictions
                ADD COLUMN confidence REAL
            """)

        conn.commit()


def prediction_exists(
    candle_time,
    model,
):
    with connect() as conn:

        row = conn.execute("""
            SELECT id
            FROM predictions
            WHERE candle_time = ?
              AND model = ?
        """, (
            candle_time,
            model,
        )).fetchone()

    return row is not None


def save_prediction(
    created_at,
    candle_time,
    candle_close,
    model,
    p_up,
    p_down,
    reason,
    context,
    predictor=None,
    model_version=None,
    confidence=None,
):
    target_candle_time = (
        candle_time + 3600
    )

    with connect() as conn:

        cursor = conn.execute("""
            INSERT INTO predictions (
                created_at,
                candle_time,
                candle_close,

                model,
                predictor,
                model_version,

                p_up,
                p_down,
                confidence,

                reason,
                context,

                target_candle_time
            )
            VALUES (
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?
            )
        """, (
            created_at,
            candle_time,
            candle_close,

            model,
            predictor,
            model_version,

            p_up,
            p_down,
            confidence,

            reason,
            context,

            target_candle_time,
        ))

        conn.commit()

        return cursor.lastrowid
