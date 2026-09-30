import os
import requests

from datetime import datetime, timezone

from dotenv import load_dotenv
from typesafe_sdk import (
    Choice,
    TypeSafeClient,
)

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


MODEL = "jev-latest"

URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/candles"
)

HEADERS = {
    "User-Agent": "btc-predict-jev/0.3"
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
    Start timestamp of the current UTC hour.

    No market data after this point may be used
    for the prediction.
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
    Keep only candles fully completed by cutoff.

    Coinbase candle timestamps represent the
    START of each candle.
    """

    return [
        candle
        for candle in candles
        if candle[0] + granularity <= cutoff
    ]


def validate_microstructure(cutoff):
    """
    Phase 3 requires a reasonably complete
    60-minute microstructure window.

    At one order-book sample every 10 seconds,
    a full hour contains about 360 samples.

    Allow some missing observations, but require
    at least 300.
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
                    "in the supplied state. "

                    "The state contains 1-hour, "
                    "15-minute, and 5-minute price and "
                    "technical information, together "
                    "with market microstructure data "
                    "including order-book imbalance, "
                    "spread, microprice offset, and "
                    "aggressive trade flow. "

                    "All supplied information is at or "
                    "before the start of the 1-hour "
                    "candle being predicted. "

                    "Do not use current market knowledge, "
                    "news, external information, or "
                    "information after the cutoff. "

                    "For order-book imbalance, 0.5 means "
                    "balanced, above 0.5 means more "
                    "bid-side depth, and below 0.5 means "
                    "more ask-side depth. "

                    "For aggressive trade flow, a "
                    "buy_ratio above 0.5 means more BTC "
                    "volume was initiated by aggressive "
                    "buyers, while below 0.5 means more "
                    "BTC volume was initiated by "
                    "aggressive sellers. "

                    "A positive microprice offset means "
                    "the microprice is shifted toward "
                    "the ask; a negative offset means "
                    "it is shifted toward the bid."
                ),

                criteria={
                    "UP": (
                        "The close of the next completed "
                        "1-hour candle is higher than the "
                        "reference close in the supplied "
                        "market snapshot."
                    ),

                    "DOWN": (
                        "The close of the next completed "
                        "1-hour candle is lower than the "
                        "reference close in the supplied "
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

    validate_microstructure(
        cutoff
    )

    #
    # Fetch all three timeframes.
    #

    candles_1h = get_candles(3600)
    candles_15m = get_candles(900)
    candles_5m = get_candles(300)

    #
    # Use exactly the same hourly cutoff
    # for every timeframe.
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
    # Phase 2 component.
    #

    price_snapshot = (
        make_multitimeframe_snapshot(
            df_1h,
            df_15m,
            df_5m,
        )
    )

    #
    # Phase 3 component.
    #
    # IMPORTANT:
    # Exactly the same cutoff is used here
    # as for the candle data.
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
    # Reference is the last completed
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

    db_model = "jev"

    #
    # Avoid duplicate API calls.
    #

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

    print()
    print("===== PHASE 3 INPUT =====")
    print(snapshot)
    print()

    result = predict(
        snapshot
    )

    p_up = result["p_up"]
    p_down = result["p_down"]

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
        "===== JEV PHASE 3 PREDICTION ====="
    )

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
        "Confidence  = "
        f"{result['confidence']:.3f}"
    )

    print(
        "Model       = "
        f"{result['actual_model']}"
    )

    created_at = datetime.now(
        timezone.utc
    ).isoformat()

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

        phase="phase3",
    )

    print()

    print(
        f"Saved prediction id="
        f"{prediction_id}"
    )


def main():
    from phase4_runner import run
    run("jev")


if __name__ == "__main__":
    main()
