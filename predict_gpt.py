import json
import requests

from datetime import datetime, timezone
from dotenv import load_dotenv
from openai import OpenAI

from indicators import (
    calculate_indicators,
    make_snapshot,
)


URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/candles"
)

HEADERS = {
    "User-Agent": "btc-predict/0.5"
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
        model="gpt-5.6-sol",
        input=prompt,
    )

    text = response.output_text.strip()

    # Remove accidental Markdown fences if present.
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

    print("===== INPUT =====")
    print(snapshot)
    print()

    result = predict(snapshot)

    print("===== GPT PREDICTION =====")

    print(
        f"P(UP)   = {result['p_up']:.3f}"
    )

    print(
        f"P(DOWN) = {result['p_down']:.3f}"
    )

    print(
        f"Reason  = {result['reason']}"
    )


if __name__ == "__main__":
    main()
