import requests

from datetime import datetime, timezone

from indicators import (
    calculate_indicators,
    make_snapshot,
)


URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/candles"
)

HEADERS = {
    "User-Agent": "btc-predict/0.4"
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

    # Coinbase returns newest first.
    # Sort oldest -> newest.
    candles.sort(key=lambda x: x[0])

    return candles


def remove_incomplete_candle(candles):
    """
    Remove the currently forming 1-hour candle.

    Example:
    If current UTC time is 12:37,
    the candle beginning at 12:00 is incomplete
    and must not be used.
    """

    now = datetime.now(timezone.utc)

    current_hour = (
        int(now.timestamp()) // 3600 * 3600
    )

    completed = [
        candle
        for candle in candles
        if candle[0] < current_hour
    ]

    return completed


def main():
    candles = get_candles()

    candles = remove_incomplete_candle(
        candles
    )

    # Use the most recent 100 completed candles.
    candles = candles[-100:]

    if len(candles) < 50:
        raise RuntimeError(
            "Not enough completed candles"
        )

    df = calculate_indicators(candles)

    snapshot = make_snapshot(df)

    print(snapshot)


if __name__ == "__main__":
    main()
