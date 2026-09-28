import pandas as pd
from datetime import datetime, timezone


def candles_to_df(candles):
    """
    Coinbase candle:
    [timestamp, low, high, open, close, volume]
    """

    df = pd.DataFrame(
        candles,
        columns=[
            "timestamp",
            "low",
            "high",
            "open",
            "close",
            "volume",
        ],
    )

    df = df.sort_values("timestamp").reset_index(drop=True)

    return df


def calculate_indicators(candles):
    df = candles_to_df(candles)

    close = df["close"]
    volume = df["volume"]

    # Returns
    df["return_1h"] = close.pct_change(1) * 100
    df["return_4h"] = close.pct_change(4) * 100
    df["return_24h"] = close.pct_change(24) * 100

    # SMA
    df["sma20"] = close.rolling(20).mean()
    df["sma50"] = close.rolling(50).mean()

    # EMA
    df["ema12"] = close.ewm(span=12, adjust=False).mean()
    df["ema26"] = close.ewm(span=26, adjust=False).mean()

    # MACD
    df["macd"] = df["ema12"] - df["ema26"]

    df["macd_signal"] = df["macd"].ewm(
        span=9,
        adjust=False,
    ).mean()

    df["macd_hist"] = (
        df["macd"] - df["macd_signal"]
    )

    # RSI(14)
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False,
    ).mean()

    rs = avg_gain / avg_loss

    df["rsi14"] = 100 - (100 / (1 + rs))

    # Bollinger Bands (20, 2 sigma)
    middle = close.rolling(20).mean()
    std = close.rolling(20).std()

    df["bb_middle"] = middle
    df["bb_upper"] = middle + 2 * std
    df["bb_lower"] = middle - 2 * std

    # Position within Bollinger Bands
    # 0 = lower band
    # 1 = upper band
    width = df["bb_upper"] - df["bb_lower"]

    df["bb_position"] = (
        (close - df["bb_lower"]) / width
    )

    # Volume relative to 20-hour average
    df["volume_avg20"] = volume.rolling(20).mean()

    df["volume_ratio"] = (
        volume / df["volume_avg20"]
    )

    return df


def make_snapshot(df):
    """
    Create deterministic text context for AI models.
    """

    x = df.iloc[-1]

    # Time of the last completed candle
    dt = datetime.fromtimestamp(
        x["timestamp"],
        tz=timezone.utc,
    )

    if x["close"] > x["sma20"]:
        sma20_position = "above"
    else:
        sma20_position = "below"

    if x["sma20"] > x["sma50"]:
        sma_trend = "SMA20 > SMA50"
    else:
        sma_trend = "SMA20 <= SMA50"

    if x["macd"] > x["macd_signal"]:
        macd_state = "MACD > signal"
    else:
        macd_state = "MACD <= signal"

    text = f"""
BTC-USD MARKET SNAPSHOT

Last completed candle:
{dt.strftime('%Y-%m-%d %H:%M')} UTC

Close:
{x['close']:.2f}

Returns:
1h: {x['return_1h']:.3f}%
4h: {x['return_4h']:.3f}%
24h: {x['return_24h']:.3f}%

Moving averages:
SMA20: {x['sma20']:.2f}
SMA50: {x['sma50']:.2f}
Price is {sma20_position} SMA20
{sma_trend}

RSI:
RSI(14): {x['rsi14']:.2f}

MACD:
MACD: {x['macd']:.2f}
Signal: {x['macd_signal']:.2f}
Histogram: {x['macd_hist']:.2f}
State: {macd_state}

Bollinger Bands:
Upper: {x['bb_upper']:.2f}
Middle: {x['bb_middle']:.2f}
Lower: {x['bb_lower']:.2f}
Position: {x['bb_position']:.3f}

Volume:
Current: {x['volume']:.2f}
20h average: {x['volume_avg20']:.2f}
Ratio: {x['volume_ratio']:.3f}
""".strip()

    return text

def calculate_timeframe_indicators(candles):
    """
    Calculate indicators that are independent of candle duration.

    Returns such as 1h / 4h are calculated separately because
    the number of candles depends on the timeframe.
    """

    df = candles_to_df(candles)

    close = df["close"]
    volume = df["volume"]

    # Moving averages
    df["sma20"] = close.rolling(20).mean()
    df["sma50"] = close.rolling(50).mean()

    # EMA / MACD
    df["ema12"] = close.ewm(
        span=12,
        adjust=False,
    ).mean()

    df["ema26"] = close.ewm(
        span=26,
        adjust=False,
    ).mean()

    df["macd"] = df["ema12"] - df["ema26"]

    df["macd_signal"] = df["macd"].ewm(
        span=9,
        adjust=False,
    ).mean()

    df["macd_hist"] = (
        df["macd"] - df["macd_signal"]
    )

    # RSI(14)
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False,
    ).mean()

    rs = avg_gain / avg_loss

    df["rsi14"] = (
        100 - (100 / (1 + rs))
    )

    # Bollinger Bands
    middle = close.rolling(20).mean()
    std = close.rolling(20).std()

    df["bb_middle"] = middle
    df["bb_upper"] = middle + 2 * std
    df["bb_lower"] = middle - 2 * std

    width = (
        df["bb_upper"] - df["bb_lower"]
    )

    df["bb_position"] = (
        (close - df["bb_lower"]) / width
    )

    # Volume ratio
    df["volume_avg20"] = (
        volume.rolling(20).mean()
    )

    df["volume_ratio"] = (
        volume / df["volume_avg20"]
    )

    return df


def pct_return(df, periods):
    """
    Percentage return over N candles.
    """

    if len(df) <= periods:
        return float("nan")

    current = df.iloc[-1]["close"]
    previous = df.iloc[-1 - periods]["close"]

    return (
        (current / previous) - 1
    ) * 100


def make_multitimeframe_snapshot(
    df_1h,
    df_15m,
    df_5m,
):
    """
    Create Phase 2 multi-timeframe snapshot.

    Prediction target remains the next completed
    1-hour BTC-USD candle.
    """

    h1 = df_1h.iloc[-1]
    m15 = df_15m.iloc[-1]
    m5 = df_5m.iloc[-1]

    dt = datetime.fromtimestamp(
        h1["timestamp"],
        tz=timezone.utc,
    )

    # Returns
    h1_r1 = pct_return(df_1h, 1)
    h1_r4 = pct_return(df_1h, 4)
    h1_r24 = pct_return(df_1h, 24)

    m15_r15 = pct_return(df_15m, 1)
    m15_r30 = pct_return(df_15m, 2)
    m15_r1h = pct_return(df_15m, 4)
    m15_r4h = pct_return(df_15m, 16)

    m5_r5 = pct_return(df_5m, 1)
    m5_r15 = pct_return(df_5m, 3)
    m5_r30 = pct_return(df_5m, 6)
    m5_r1h = pct_return(df_5m, 12)

    #
    # Last 12 completed 5-minute candles
    #

    candle_lines = []

    for _, row in df_5m.tail(12).iterrows():

        candle_dt = datetime.fromtimestamp(
            row["timestamp"],
            tz=timezone.utc,
        )

        candle_lines.append(
            f"{candle_dt.strftime('%H:%M')} "
            f"O={row['open']:.2f} "
            f"H={row['high']:.2f} "
            f"L={row['low']:.2f} "
            f"C={row['close']:.2f} "
            f"V={row['volume']:.2f}"
        )

    recent_5m = "\n".join(candle_lines)

    text = f"""
BTC-USD MULTI-TIMEFRAME MARKET SNAPSHOT

Prediction target:
Direction of the next completed 1-hour candle.

Reference completed 1-hour candle:
{dt.strftime('%Y-%m-%d %H:%M')} UTC

Reference close:
{h1['close']:.2f}


=== 1-HOUR CONTEXT ===

Returns:
1h: {h1_r1:.3f}%
4h: {h1_r4:.3f}%
24h: {h1_r24:.3f}%

Moving averages:
SMA20: {h1['sma20']:.2f}
SMA50: {h1['sma50']:.2f}

RSI(14):
{h1['rsi14']:.2f}

MACD:
MACD: {h1['macd']:.2f}
Signal: {h1['macd_signal']:.2f}
Histogram: {h1['macd_hist']:.2f}

Bollinger position:
{h1['bb_position']:.3f}

Volume ratio:
{h1['volume_ratio']:.3f}


=== 15-MINUTE CONTEXT ===

Returns:
15m: {m15_r15:.3f}%
30m: {m15_r30:.3f}%
1h: {m15_r1h:.3f}%
4h: {m15_r4h:.3f}%

RSI(14):
{m15['rsi14']:.2f}

MACD:
MACD: {m15['macd']:.2f}
Signal: {m15['macd_signal']:.2f}
Histogram: {m15['macd_hist']:.2f}

Bollinger position:
{m15['bb_position']:.3f}

Volume ratio:
{m15['volume_ratio']:.3f}


=== 5-MINUTE CONTEXT ===

Returns:
5m: {m5_r5:.3f}%
15m: {m5_r15:.3f}%
30m: {m5_r30:.3f}%
1h: {m5_r1h:.3f}%

RSI(14):
{m5['rsi14']:.2f}

MACD:
MACD: {m5['macd']:.2f}
Signal: {m5['macd_signal']:.2f}
Histogram: {m5['macd_hist']:.2f}

Volume ratio:
{m5['volume_ratio']:.3f}

Recent completed 5-minute candles:
{recent_5m}
""".strip()

    return text
