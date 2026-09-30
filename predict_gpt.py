import json
import requests

from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import OpenAI

from indicators import (
    calculate_timeframe_indicators,
    make_multitimeframe_snapshot,
)

from microstructure import (
    make_microstructure_snapshot,
    get_orderbook_average,
    get_trade_flow,
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
    "User-Agent": "btc-predict/0.8"
}


def get_candles(granularity):
    r = requests.get(
        URL,
        params={
            "granularity": granularity,
        },
        headers=HEADERS,
        timeout=10,
    )

    r.raise_for_status()

    candles = r.json()
    candles.sort(key=lambda x: x[0])

    return candles


def get_prediction_cutoff():
    """
    Return the start timestamp of the current UTC hour.

    All market data used for the prediction must have
    completed by this timestamp.
    """

    now = datetime.now(timezone.utc)

    return (
        int(now.timestamp()) // 3600 * 3600
    )


def remove_after_cutoff(
    candles,
    granularity,
    cutoff,
):
    """
    Keep only candles that are fully completed at cutoff.

    Coinbase candle timestamp is the START time of
    the candle, so:

        candle_start + granularity <= cutoff
    """

    return [
        candle
        for candle in candles
        if candle[0] + granularity <= cutoff
    ]


def validate_microstructure(cutoff):
    """
    Make sure Phase 3 has enough microstructure data
    before spending an API call.

    With a 10-second order-book sampling interval,
    a complete 60-minute window should contain about
    360 samples.

    We allow some missing samples and require at least
    300.
    """

    orderbook_60m = get_orderbook_average(
        cutoff,
        60,
    )

    if orderbook_60m is None:
        raise RuntimeError(
            "No 60-minute order book data available"
        )

    samples = orderbook_60m["samples"]

    if samples < 300:
        raise RuntimeError(
            "Insufficient 60-minute order book data: "
            f"{samples} samples "
            "(need at least 300)"
        )

    trade_60m = get_trade_flow(
        cutoff,
        60,
    )

    if trade_60m is None:
        raise RuntimeError(
            "No 60-minute trade flow data available"
        )

    if trade_60m["trades"] < 1:
        raise RuntimeError(
            "No trades in 60-minute window"
        )

    print(
        "Microstructure validation: OK"
    )

    print(
        f"  order book samples: {samples}"
    )

    print(
        f"  trades: "
        f"{trade_60m['trades']}"
    )


def predict(snapshot):
    client = OpenAI()

    prompt = f"""
You are predicting the direction of BTC-USD
over the next completed 1-hour candle.

Use ONLY the market information supplied below.

The market snapshot contains:

1. Price and technical information from
   1-hour, 15-minute, and 5-minute timeframes.

2. Market microstructure information including
   order-book imbalance, spread, microprice,
   and aggressive trade flow.

All supplied information is at or before the
start of the 1-hour candle being predicted.

Do not use current market knowledge,
news, external information, or information
after the timestamp cutoff in the snapshot.

For order-book imbalance:

0.5 means balanced.
Above 0.5 means more bid-side depth.
Below 0.5 means more ask-side depth.

For aggressive trade flow:

buy_ratio above 0.5 means more BTC volume
was initiated by aggressive buyers.

buy_ratio below 0.5 means more BTC volume
was initiated by aggressive sellers.

Microprice offset is:

microprice - midpoint

A positive value indicates the microprice
is shifted toward the ask.
A negative value indicates it is shifted
toward the bid.

UP means:
the close of the next completed 1-hour candle
is higher than the reference close shown
in the snapshot.

DOWN means:
the close of the next completed 1-hour candle
is lower than the reference close shown
in the snapshot.

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


def phase3_main():
    load_dotenv()

    init_db()

    cutoff = get_prediction_cutoff()

    cutoff_dt = datetime.fromtimestamp(
        cutoff,
        tz=timezone.utc,
    )

    print(
        "Prediction cutoff:",
        cutoff_dt.strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
    )

    #
    # Phase 3 safety check.
    #
    # Do this before calling the model.
    #

    validate_microstructure(
        cutoff
    )

    #
    # Fetch all three candle timeframes.
    #

    candles_1h = get_candles(3600)
    candles_15m = get_candles(900)
    candles_5m = get_candles(300)

    #
    # IMPORTANT:
    # All timeframes use the SAME hourly cutoff.
    #

    candles_1h = remove_after_cutoff(
        candles_1h,
        3600,
        cutoff,
    )

    candles_15m = remove_after_cutoff(
        candles_15m,
        900,
        cutoff,
    )

    candles_5m = remove_after_cutoff(
        candles_5m,
        300,
        cutoff,
    )

    if len(candles_1h) < 50:
        raise RuntimeError(
            "Not enough completed 1h candles"
        )

    if len(candles_15m) < 50:
        raise RuntimeError(
            "Not enough completed 15m candles"
        )

    if len(candles_5m) < 50:
        raise RuntimeError(
            "Not enough completed 5m candles"
        )

    #
    # Keep enough history for indicators.
    #

    candles_1h = candles_1h[-100:]
    candles_15m = candles_15m[-200:]
    candles_5m = candles_5m[-300:]

    df_1h = calculate_timeframe_indicators(
        candles_1h
    )

    df_15m = calculate_timeframe_indicators(
        candles_15m
    )

    df_5m = calculate_timeframe_indicators(
        candles_5m
    )

    #
    # Phase 2 component:
    # multi-timeframe OHLCV / indicators.
    #

    price_snapshot = (
        make_multitimeframe_snapshot(
            df_1h,
            df_15m,
            df_5m,
        )
    )

    #
    # Phase 3 component:
    # order book + aggressive trade flow.
    #
    # IMPORTANT:
    # Use exactly the SAME cutoff.
    #

    micro_snapshot = (
        make_microstructure_snapshot(
            cutoff_dt
        )
    )

    snapshot = (
        price_snapshot
        + "\n\n"
        + micro_snapshot
    )

    #
    # Reference candle is the last completed
    # 1-hour candle.
    #

    latest = df_1h.iloc[-1]

    candle_time = int(
        latest["timestamp"]
    )

    candle_close = float(
        latest["close"]
    )

    #
    # Sanity check.
    #

    expected_candle_time = (
        cutoff - 3600
    )

    if candle_time != expected_candle_time:
        raise RuntimeError(
            "Unexpected reference candle: "
            f"expected={expected_candle_time}, "
            f"actual={candle_time}"
        )

    #
    # Avoid duplicate API calls.
    #

    if prediction_exists(
        candle_time,
        MODEL,
    ):
        print(
            f"Prediction already exists: "
            f"candle_time={candle_time}, "
            f"model={MODEL}"
        )

        print("Skipping.")
        return

    print()
    print("===== PHASE 3 INPUT =====")
    print(snapshot)
    print()

    result = predict(snapshot)

    p_up = float(
        result["p_up"]
    )

    p_down = float(
        result["p_down"]
    )

    reason = result.get(
        "reason",
        "",
    )

    #
    # Validation
    #

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

    print(
        "===== GPT PHASE 3 PREDICTION ====="
    )

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
        predictor="openai",
        model_version=MODEL,

        p_up=p_up,
        p_down=p_down,

        confidence=None,

        reason=reason,
        context=snapshot,

        phase="phase3",
    )

    print()

    print(
        f"Saved prediction id="
        f"{prediction_id}"
    )


def main():
    from phase4_runner import run
    run("openai")


if __name__ == "__main__":
    main()
