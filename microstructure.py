#!/usr/bin/env python3

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(__file__).parent / "market.db"


def connect():
    return sqlite3.connect(DB_PATH)


def _cutoff_timestamp(cutoff):
    """
    cutoff:
      datetime (timezone-aware推奨)
      または Unix timestamp
    """

    if isinstance(cutoff, datetime):
        return cutoff.timestamp()

    return float(cutoff)


def _fmt(value, digits=3):
    if value is None:
        return "N/A"

    return f"{value:.{digits}f}"


# =========================================================
# Order book
# =========================================================

def get_current_orderbook(cutoff):
    """
    cutoff以前で最も新しいorder book sample。
    """

    ts = _cutoff_timestamp(cutoff)

    with connect() as conn:
        conn.row_factory = sqlite3.Row

        row = conn.execute("""
            SELECT *
            FROM orderbook_samples
            WHERE timestamp <= ?
            ORDER BY timestamp DESC
            LIMIT 1
        """, (ts,)).fetchone()

    if row is None:
        return None

    return dict(row)


def get_orderbook_average(cutoff, minutes):
    """
    cutoff直前minutes分の平均。
    """

    end_ts = _cutoff_timestamp(cutoff)
    start_ts = end_ts - minutes * 60

    with connect() as conn:
        conn.row_factory = sqlite3.Row

        row = conn.execute("""
            SELECT
                COUNT(*) AS samples,

                AVG(spread)
                    AS spread,

                AVG(top5_imbalance)
                    AS top5_imbalance,

                AVG(top10_imbalance)
                    AS top10_imbalance,

                AVG(top20_imbalance)
                    AS top20_imbalance,

                AVG(microprice_offset)
                    AS microprice_offset

            FROM orderbook_samples

            WHERE timestamp > ?
              AND timestamp <= ?
        """, (
            start_ts,
            end_ts,
        )).fetchone()

    if row["samples"] == 0:
        return None

    return dict(row)


# =========================================================
# Trade flow
# =========================================================

def get_trade_flow(cutoff, minutes):
    """
    Aggressor-side trade flow for the period immediately
    preceding cutoff.
    """

    end_ts = _cutoff_timestamp(cutoff)
    start_ts = end_ts - minutes * 60

    with connect() as conn:
        conn.row_factory = sqlite3.Row

        row = conn.execute("""
            SELECT
                COUNT(*) AS trades,

                SUM(
                    CASE
                    WHEN aggressor_side = 'buy'
                    THEN size
                    ELSE 0
                    END
                ) AS buy_btc,

                SUM(
                    CASE
                    WHEN aggressor_side = 'sell'
                    THEN size
                    ELSE 0
                    END
                ) AS sell_btc,

                SUM(
                    CASE
                    WHEN aggressor_side = 'buy'
                    THEN quote_volume
                    ELSE 0
                    END
                ) AS buy_usd,

                SUM(
                    CASE
                    WHEN aggressor_side = 'sell'
                    THEN quote_volume
                    ELSE 0
                    END
                ) AS sell_usd

            FROM trades

            WHERE timestamp > ?
              AND timestamp <= ?
        """, (
            start_ts,
            end_ts,
        )).fetchone()

    if row["trades"] == 0:
        return None

    result = dict(row)

    buy_btc = result["buy_btc"] or 0.0
    sell_btc = result["sell_btc"] or 0.0

    total_btc = buy_btc + sell_btc

    if total_btc > 0:
        result["buy_ratio"] = (
            buy_btc / total_btc
        )
    else:
        result["buy_ratio"] = 0.5

    buy_usd = result["buy_usd"] or 0.0
    sell_usd = result["sell_usd"] or 0.0

    total_usd = buy_usd + sell_usd

    if total_usd > 0:
        result["buy_ratio_usd"] = (
            buy_usd / total_usd
        )
    else:
        result["buy_ratio_usd"] = 0.5

    return result


# =========================================================
# Snapshot
# =========================================================

def make_microstructure_snapshot(cutoff):
    current = get_current_orderbook(cutoff)

    windows = [
        1,
        5,
        15,
        60,
    ]

    orderbook = {
        minutes:
        get_orderbook_average(
            cutoff,
            minutes,
        )
        for minutes in windows
    }

    trades = {
        minutes:
        get_trade_flow(
            cutoff,
            minutes,
        )
        for minutes in windows
    }

    lines = []

    lines.append(
        "=== MARKET MICROSTRUCTURE ==="
    )

    lines.append(
        "All data below is at or before "
        "the prediction cutoff."
    )

    lines.append("")

    #
    # Current order book
    #

    lines.append(
        "--- ORDER BOOK: CURRENT ---"
    )

    if current is None:
        lines.append(
            "No order book data available."
        )

    else:
        sample_time = (
            datetime.fromtimestamp(
                current["timestamp"],
                timezone.utc,
            )
        )

        lines.append(
            "Sample time: "
            f"{sample_time.isoformat()}"
        )

        lines.append(
            "Best bid: "
            f"{current['best_bid']:.2f}"
        )

        lines.append(
            "Best ask: "
            f"{current['best_ask']:.2f}"
        )

        lines.append(
            "Spread: "
            f"{current['spread']:.2f}"
        )

        lines.append(
            "Top 5 imbalance: "
            f"{current['top5_imbalance']:.3f}"
        )

        lines.append(
            "Top 10 imbalance: "
            f"{current['top10_imbalance']:.3f}"
        )

        lines.append(
            "Top 20 imbalance: "
            f"{current['top20_imbalance']:.3f}"
        )

        lines.append(
            "Microprice offset: "
            f"{current['microprice_offset']:+.4f}"
        )

    lines.append("")

    #
    # Averaged order book
    #

    lines.append(
        "--- ORDER BOOK: AVERAGES ---"
    )

    for minutes in windows:
        data = orderbook[minutes]

        if data is None:
            lines.append(
                f"{minutes}m: N/A"
            )
            continue

        lines.append(
            f"{minutes}m: "
            f"samples={data['samples']}, "
            f"spread={_fmt(data['spread'], 3)}, "
            f"imb5={_fmt(data['top5_imbalance'], 3)}, "
            f"imb10={_fmt(data['top10_imbalance'], 3)}, "
            f"imb20={_fmt(data['top20_imbalance'], 3)}, "
            f"micro_offset="
            f"{data['microprice_offset']:+.4f}"
        )

    lines.append("")

    #
    # Trade flow
    #

    lines.append(
        "--- AGGRESSIVE TRADE FLOW ---"
    )

    for minutes in windows:
        data = trades[minutes]

        if data is None:
            lines.append(
                f"{minutes}m: N/A"
            )
            continue

        lines.append(
            f"{minutes}m: "
            f"trades={data['trades']}, "
            f"buy={data['buy_btc']:.4f} BTC, "
            f"sell={data['sell_btc']:.4f} BTC, "
            f"buy_ratio={data['buy_ratio']:.3f}"
        )

    return "\n".join(lines)


# =========================================================
# Test
# =========================================================

def main():
    #
    # Use the most recent completed UTC hour.
    #
    now = datetime.now(
        timezone.utc
    )

    cutoff = now.replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    print(
        "Cutoff:",
        cutoff.isoformat(),
    )

    print()

    print(
        make_microstructure_snapshot(
            cutoff
        )
    )


if __name__ == "__main__":
    main()
