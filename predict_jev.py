import os
import requests

from datetime import datetime, timezone

from dotenv import load_dotenv
from typesafe_sdk import (
    Choice,
    TypeSafeClient,
)

from indicators import (
    calculate_indicators,
    make_snapshot,
)

from db import (
    init_db,
    save_prediction,
    prediction_exists,
)


MODEL = "jev-latest"

URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/candles"
)

HEADERS = {
    "User-Agent": "btc-predict-jev/0.1"
}


def get_candles():
    r = requests.get(
        URL,
        params={
            "granularity": 3600,
        },
        headers=HEADERS,
        timeout=10,
    )

    r.raise_for_status()

    candles = r.json()

    # Oldest -> newest
    candles.sort(key=lambda x: x[0])

    return candles


def remove_incomplete_candle(candles):
    now = datetime.now(timezone.utc)

    current_hour = (
        int(now.timestamp())
        // 3600
        * 3600
    )

    return [
        candle
        for candle in candles
        if candle[0] < current_hour
    ]


def predict(snapshot):
    client = TypeSafeClient(
        api_key=os.environ["TYPESAFE_API_KEY"],
        timeout=120.0,
    )

    response = client.system_one(
        model=MODEL,

        state=snapshot,

        questions={
            "direction": Choice(
                instructions=(
                    "Predict the direction of BTC-USD "
                    "over the next completed 1-hour "
                    "candle using only the market data "
                    "in the supplied state."
                ),

                criteria={
                    "UP": (
                        "The close of the next completed "
                        "1-hour candle is higher than the "
                        "Close value in the supplied "
                        "market snapshot."
                    ),

                    "DOWN": (
                        "The close of the next completed "
                        "1-hour candle is lower than the "
                        "Close value in the supplied "
                        "market snapshot."
                    ),
                },
            )
        },
    )

    answer = response.answers["direction"]

    return {
        "p_up": float(
            answer.probabilities["UP"]
        ),

        "p_down": float(
            answer.probabilities["DOWN"]
        ),

        "choice": answer.choice,

        "confidence": float(
            answer.confidence
        ),

        "actual_model": response.model,
    }


def main():
    load_dotenv()

    init_db()

    candles = get_candles()

    candles = remove_incomplete_candle(
        candles
    )

    candles = candles[-100:]

    if len(candles) < 50:
        raise RuntimeError(
            "Not enough completed candles"
        )

    df = calculate_indicators(
        candles
    )

    snapshot = make_snapshot(
        df
    )

    latest = df.iloc[-1]

    candle_time = int(
        latest["timestamp"]
    )

    candle_close = float(
        latest["close"]
    )

    #
    # Use a separate model name in the DB.
    #
    # This allows GPT and Jev predictions
    # for the same candle.
    #
    db_model = "jev"

    if prediction_exists(
        candle_time,
        db_model,
    ):
        print(
            "Jev prediction already exists "
            f"for candle_time={candle_time}"
        )

        print("Skipping.")

        return

    print("===== INPUT =====")
    print(snapshot)
    print()

    result = predict(
        snapshot
    )

    p_up = result["p_up"]
    p_down = result["p_down"]

    if not 0 <= p_up <= 1:
        raise ValueError(
            f"Invalid p_up: {p_up}"
        )

    if not 0 <= p_down <= 1:
        raise ValueError(
            f"Invalid p_down: {p_down}"
        )

    if abs(
        (p_up + p_down) - 1.0
    ) > 0.01:
        raise ValueError(
            "Probabilities do not sum to 1"
        )

    print("===== JEV PREDICTION =====")

    print(
        f"P(UP)       = {p_up:.3f}"
    )

    print(
        f"P(DOWN)     = {p_down:.3f}"
    )

    print(
        f"Choice      = {result['choice']}"
    )

    print(
        f"Confidence  = "
        f"{result['confidence']:.3f}"
    )

    print(
        f"Model       = "
        f"{result['actual_model']}"
    )

    created_at = datetime.now(
        timezone.utc
    ).isoformat()

    #
    # Jev does not generate a textual reason.
    # Store useful metadata in the existing
    # reason column for now.
    #
    reason = (
        f"Jev choice={result['choice']}; "
        f"confidence={result['confidence']:.4f}; "
        f"model={result['actual_model']}"
    )

    prediction_id = save_prediction(
        created_at=created_at,
        candle_time=candle_time,
        candle_close=candle_close,

        model=db_model,
        predictor="jev",
        model_version=result["actual_model"],

        p_up=p_up,
        p_down=p_down,

        confidence=result["confidence"],

        reason=reason,
        context=snapshot,
    )      

    print()
    print(
        f"Saved prediction id="
        f"{prediction_id}"
    )


if __name__ == "__main__":
    main()
