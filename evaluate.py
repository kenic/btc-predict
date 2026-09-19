import sqlite3
import requests

from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(__file__).parent / "btc.db"

BASE_URL = "https://api.exchange.coinbase.com"

HEADERS = {
    "User-Agent": "btc-predict/0.8"
}


def connect():
    return sqlite3.connect(DB_PATH)


def get_pending_predictions():
    """
    Return predictions that have not yet been evaluated.
    """

    with connect() as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT
                id,
                candle_time,
                candle_close,
                target_candle_time,
                p_up,
                p_down,
                model
            FROM predictions
            WHERE evaluated_at IS NULL
            ORDER BY target_candle_time
        """).fetchall()

    return rows


def target_candle_is_complete(target_candle_time):
    """
    A candle beginning at target_candle_time is complete
    only after target_candle_time + 3600.
    """

    now = int(
        datetime.now(timezone.utc).timestamp()
    )

    return now >= target_candle_time + 3600


def get_target_candle(target_candle_time):
    """
    Fetch the completed Coinbase candle beginning
    exactly at target_candle_time.

    Coinbase format:
    [timestamp, low, high, open, close, volume]
    """

    url = (
        f"{BASE_URL}/products/"
        "BTC-USD/candles"
    )

    start = datetime.fromtimestamp(
        target_candle_time,
        tz=timezone.utc,
    )

    end = datetime.fromtimestamp(
        target_candle_time + 7200,
        tz=timezone.utc,
    )

    params = {
        "granularity": 3600,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }

    r = requests.get(
        url,
        params=params,
        headers=HEADERS,
        timeout=10,
    )

    r.raise_for_status()

    candles = r.json()

    for candle in candles:
        if int(candle[0]) == target_candle_time:
            return candle

    return None


def evaluate_prediction(row):
    prediction_id = row["id"]

    old_close = float(
        row["candle_close"]
    )

    target_time = int(
        row["target_candle_time"]
    )

    if not target_candle_is_complete(target_time):
        return False

    candle = get_target_candle(
        target_time
    )

    if candle is None:
        print(
            f"id={prediction_id}: "
            "target candle not available"
        )
        return False

    actual_close = float(
        candle[4]
    )

    if actual_close > old_close:
        actual_direction = "UP"

    elif actual_close < old_close:
        actual_direction = "DOWN"

    else:
        actual_direction = "FLAT"

    predicted_direction = (
        "UP"
        if row["p_up"] >= row["p_down"]
        else "DOWN"
    )

    if actual_direction == "FLAT":
        correct = None

    else:
        correct = int(
            predicted_direction
            == actual_direction
        )

    evaluated_at = datetime.now(
        timezone.utc
    ).isoformat()

    with connect() as conn:

        conn.execute("""
            UPDATE predictions
            SET
                actual_close = ?,
                actual_direction = ?,
                correct = ?,
                evaluated_at = ?
            WHERE id = ?
        """, (
            actual_close,
            actual_direction,
            correct,
            evaluated_at,
            prediction_id,
        ))

        conn.commit()

    change = (
        (actual_close / old_close) - 1
    ) * 100

    print(
        f"id={prediction_id} "
        f"{row['model']} "
        f"prediction={predicted_direction} "
        f"actual={actual_direction} "
        f"{old_close:.2f} -> "
        f"{actual_close:.2f} "
        f"({change:+.3f}%) "
        f"correct={correct}"
    )

    return True


def show_statistics():
    """
    Show accuracy and Brier score.

    For binary UP/DOWN prediction:

        outcome = 1 if UP
        outcome = 0 if DOWN

        Brier = (p_up - outcome)^2

    Lower Brier score is better.
    """

    with connect() as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT
                model,
                p_up,
                p_down,
                actual_direction,
                correct
            FROM predictions
            WHERE
                actual_direction IN ('UP', 'DOWN')
        """).fetchall()

    if not rows:
        print()
        print("No evaluated predictions yet.")
        return

    models = {}

    for row in rows:

        model = row["model"]

        if model not in models:
            models[model] = {
                "n": 0,
                "hits": 0,
                "brier_sum": 0.0,
                "confidence_sum": 0.0,
            }

        stats = models[model]

        p_up = float(
            row["p_up"]
        )

        p_down = float(
            row["p_down"]
        )

        if row["actual_direction"] == "UP":
            outcome = 1.0
        else:
            outcome = 0.0

        brier = (
            p_up - outcome
        ) ** 2

        confidence = max(
            p_up,
            p_down,
        )

        stats["n"] += 1

        if row["correct"] == 1:
            stats["hits"] += 1

        stats["brier_sum"] += brier

        stats["confidence_sum"] += (
            confidence
        )

    print()
    print("===== STATISTICS =====")

    for model, stats in models.items():

        n = stats["n"]

        accuracy = (
            stats["hits"] / n
        )

        brier = (
            stats["brier_sum"] / n
        )

        avg_confidence = (
            stats["confidence_sum"] / n
        )

        print()
        print(model)

        print(
            f"  N:              {n}"
        )

        print(
            f"  Accuracy:       "
            f"{accuracy * 100:.1f}%"
        )

        print(
            f"  Brier score:    "
            f"{brier:.4f}"
        )

        print(
            f"  Avg confidence: "
            f"{avg_confidence * 100:.1f}%"
        )


def main():

    rows = get_pending_predictions()

    if not rows:

        print(
            "No pending predictions."
        )

        show_statistics()

        return

    print(
        f"Pending predictions: "
        f"{len(rows)}"
    )

    evaluated = 0

    for row in rows:

        try:

            if evaluate_prediction(row):
                evaluated += 1

        except Exception as e:

            print(
                f"id={row['id']}: "
                f"ERROR: {e}"
            )

    print(
        f"Evaluated this run: "
        f"{evaluated}"
    )

    show_statistics()


if __name__ == "__main__":
    main()
