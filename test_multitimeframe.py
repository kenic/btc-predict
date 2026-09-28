#!/usr/bin/env python3

from datetime import datetime, timezone

from predict_gpt import (
    get_candles,
    get_prediction_cutoff,
    remove_after_cutoff,
)

from indicators import (
    calculate_timeframe_indicators,
    make_multitimeframe_snapshot,
)


def fmt(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M UTC")


def main():
    cutoff = get_prediction_cutoff()

    print("===== TIME ALIGNMENT TEST =====")
    print()
    print(f"Cutoff: {fmt(cutoff)}")
    print()

    configs = [
        ("1h", 3600),
        ("15m", 900),
        ("5m", 300),
    ]

    data = {}

    for name, granularity in configs:
        candles = get_candles(granularity)

        candles = remove_after_cutoff(
            candles,
            granularity,
            cutoff,
        )

        if not candles:
            raise RuntimeError(
                f"No completed {name} candles"
            )

        data[name] = candles

        last = candles[-1]

        print(
            f"{name:3} last start: "
            f"{fmt(last[0])}"
        )

        print(
            f"{name:3} last end:   "
            f"{fmt(last[0] + granularity)}"
        )

        print(
            f"{name:3} candles:    "
            f"{len(candles)}"
        )

        print()

    #
    # Verify that all three timeframes end
    # exactly at the same hourly cutoff.
    #

    for name, granularity in configs:
        last = data[name][-1]

        end_time = (
            last[0] + granularity
        )

        if end_time != cutoff:
            raise RuntimeError(
                f"{name} alignment error: "
                f"last candle ends at "
                f"{fmt(end_time)}, "
                f"expected {fmt(cutoff)}"
            )

    print("Time alignment: OK")
    print()

    #
    # Calculate indicators.
    #

    df_1h = calculate_timeframe_indicators(
        data["1h"][-100:]
    )

    df_15m = calculate_timeframe_indicators(
        data["15m"][-200:]
    )

    df_5m = calculate_timeframe_indicators(
        data["5m"][-300:]
    )

    snapshot = make_multitimeframe_snapshot(
        df_1h,
        df_15m,
        df_5m,
    )

    print("===== SNAPSHOT =====")
    print()
    print(snapshot)
    print()
    print("===== END =====")


if __name__ == "__main__":
    main()
