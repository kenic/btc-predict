import json
import requests

from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import OpenAI

from indicators import (
    calculate_indicators,
    make_snapshot,
)

from db import (
    init_db,
    save_prediction,
    prediction_exists,
)


MODEL = "gpt-5.6-sol"

URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/candles"
)

HEADERS = {
    "User-Agent": "btc-predict/0.6"
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
    candles.sort(key=lambda x: x[0])

    return candles


def remove_incomplete_candle(candles):
    now = datetime.now(timezone.utc)

    current_hour = (
        int(now.timestamp()) // 3600 * 3600
    )

    return [
        candle
        for candle in candles
        if candle[0] < current_hour
    ]


def predict(snapshot):
    client = OpenAI()

    prompt = f"""
You are predicting the direction of BTC-USD
over the next completed 1-hour candle.

Use ONLY the market information supplied below.

Do not use current market knowledge,
news, external information, or information
after the timestamp in the snapshot.

UP means:
the close of the next completed 1-hour candle
is higher than the close shown in the snapshot.

DOWN means:
the close of the next completed 1-hour candle
is lower than the close shown in the snapshot.

Estimate:

1. Probability of UP
2. Probability of DOWN

The probabilities must sum to 1.

Return JSON only in exactly this form:

{{
  "p_up": 0.50,
  "p_down": 0.50,
  "reason": "short explanation"
}}

MARKET DATA:

{snapshot}
""".strip()

    response = client.responses.create(
        model=MODEL,
        input=prompt,
    )

    text = response.output_text.strip()

    if text.startswith("```"):
        lines = text.splitlines()

        if lines[0].startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]

        text = "\n".join(lines)

    return json.loads(text)


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

    df = calculate_indicators(candles)

    snapshot = make_snapshot(df)

    # Last completed candle
    latest = df.iloc[-1]

    candle_time = int(
        latest["timestamp"]
    )

    candle_close = float(
        latest["close"]
    )

    # Do not call the API again if this candle
    # has already been predicted by this model.
    if prediction_exists(candle_time, MODEL):
        print(
            f"Prediction already exists: "
            f"candle_time={candle_time}, "
            f"model={MODEL}"
        )
        print("Skipping.")
        return

    print("===== INPUT =====")
    print(snapshot)
    print()

    result = predict(snapshot)

    p_up = float(result["p_up"])
    p_down = float(result["p_down"])
    reason = result.get("reason", "")

    # Basic validation
    if not 0 <= p_up <= 1:
        raise ValueError(
            f"Invalid p_up: {p_up}"
        )

    if not 0 <= p_down <= 1:
        raise ValueError(
            f"Invalid p_down: {p_down}"
        )

    if abs((p_up + p_down) - 1.0) > 0.01:
        raise ValueError(
            "Probabilities do not sum to 1"
        )

    print("===== GPT PREDICTION =====")

    print(
        f"P(UP)   = {p_up:.3f}"
    )

    print(
        f"P(DOWN) = {p_down:.3f}"
    )

    print(
        f"Reason  = {reason}"
    )

    created_at = datetime.now(
        timezone.utc
    ).isoformat()

    prediction_id = save_prediction(
        created_at=created_at,
        candle_time=candle_time,
        candle_close=candle_close,
        model=MODEL,
        p_up=p_up,
        p_down=p_down,
        reason=reason,
        context=snapshot,
    )

    print()
    print(
        f"Saved prediction id={prediction_id}"
    )


if __name__ == "__main__":
    main()
