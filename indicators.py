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
