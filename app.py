from ui import page_shell
import html
import math
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import requests
from flask import Flask, abort, request


app = Flask(__name__)

DB_PATH = Path(__file__).parent / "btc.db"

COINBASE_URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/ticker"
)

HEADERS = {
    "User-Agent": "btc-predict-dashboard/0.3"
}

INITIAL_BALANCE = 10000.0
CURRENT_PHASE = "phase3"
MARKET_DB_PATH = Path(__file__).parent / "market.db"
PHASE3_SUBTITLE = "Market microstructure: 1h + 15m + 5m + order book + trade flow"


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_virtual_trading(predictor, phase):
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                candle_time,
                p_up,
                p_down,
                actual_return
            FROM predictions
            WHERE predictor = ?
              AND phase = ?
              AND actual_return IS NOT NULL
            ORDER BY candle_time ASC
        """, (
            predictor,
            phase,
        )).fetchall()

    balance = INITIAL_BALANCE
    trades = 0

    for row in rows:
        p_up = float(row["p_up"])

        actual_return = (
            float(row["actual_return"]) / 100.0
        )

        # +1.0 = 100% long
        # -1.0 = 100% short
        #  0.0 = no position
        position = 2.0 * p_up - 1.0

        balance *= (
            1.0 + position * actual_return
        )

        trades += 1

    total_return = (
        (balance / INITIAL_BALANCE) - 1.0
    ) * 100.0

    return {
        "initial_balance": INITIAL_BALANCE,
        "balance": balance,
        "return": total_return,
        "trades": trades,
    }


def get_btc_price():
    r = requests.get(
        COINBASE_URL,
        headers=HEADERS,
        timeout=10,
    )

    r.raise_for_status()

    return float(r.json()["price"])


def format_time(timestamp):
    if timestamp is None:
        return "-"

    dt = datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc,
    )

    return dt.strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def get_latest_battle():
    """
    Find the latest candle_time for which
    both GPT and Jev predictions exist.
    """

    with connect() as conn:
        row = conn.execute("""
            SELECT candle_time
            FROM predictions
            GROUP BY candle_time
            HAVING
                SUM(
                    CASE
                    WHEN predictor = 'openai'
                    THEN 1
                    ELSE 0
                    END
                ) > 0
                AND
                SUM(
                    CASE
                    WHEN predictor = 'jev'
                    THEN 1
                    ELSE 0
                    END
                ) > 0
            ORDER BY candle_time DESC
            LIMIT 1
        """).fetchone()

        if row is None:
            return None, None

        candle_time = row["candle_time"]

        rows = conn.execute("""
            SELECT *
            FROM predictions
            WHERE candle_time = ?
            ORDER BY predictor
        """, (
            candle_time,
        )).fetchall()

    gpt = None
    jev = None

    for row in rows:
        if row["predictor"] == "openai":
            gpt = row

        elif row["predictor"] == "jev":
            jev = row

    return gpt, jev


def get_model_statistics(predictor, phase):
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                p_up,
                p_down,
                actual_direction,
                correct
            FROM predictions
            WHERE predictor = ?
              AND phase = ?
              AND actual_direction IN ('UP', 'DOWN')
        """, (
            predictor,
            phase,
        )).fetchall()

    n = len(rows)

    if n == 0:
        return {
            "n": 0,
            "wins": 0,
            "accuracy": None,
            "brier": None,
            "strength": None,
        }

    wins = 0
    brier_sum = 0.0
    strength_sum = 0.0

    for row in rows:
        p_up = float(row["p_up"])
        p_down = float(row["p_down"])

        if row["actual_direction"] == "UP":
            outcome = 1.0
        else:
            outcome = 0.0

        brier_sum += (
            p_up - outcome
        ) ** 2

        strength_sum += max(
            p_up,
            p_down,
        )

        if row["correct"] == 1:
            wins += 1

    return {
        "n": n,
        "wins": wins,
        "accuracy": wins / n,
        "brier": brier_sum / n,
        "strength": strength_sum / n,
    }


def get_recent_battles(limit=12):
    """
    Return recent candle_times having
    both GPT and Jev predictions.
    """

    with connect() as conn:
        times = conn.execute("""
            SELECT candle_time
            FROM predictions
            GROUP BY candle_time
            HAVING
                SUM(
                    CASE
                    WHEN predictor = 'openai'
                    THEN 1
                    ELSE 0
                    END
                ) > 0
                AND
                SUM(
                    CASE
                    WHEN predictor = 'jev'
                    THEN 1
                    ELSE 0
                    END
                ) > 0
            ORDER BY candle_time DESC
            LIMIT ?
        """, (
            limit,
        )).fetchall()

        battles = []

        for item in times:
            candle_time = item["candle_time"]

            rows = conn.execute("""
                SELECT *
                FROM predictions
                WHERE candle_time = ?
            """, (
                candle_time,
            )).fetchall()

            gpt = None
            jev = None

            for row in rows:
                if row["predictor"] == "openai":
                    gpt = row

                elif row["predictor"] == "jev":
                    jev = row

            if gpt and jev:
                battles.append(
                    (gpt, jev)
                )

    return battles


def direction(row):
    if row is None:
        return "-"

    if float(row["p_up"]) >= float(row["p_down"]):
        return "UP"

    return "DOWN"


def direction_arrow(row):
    if direction(row) == "UP":
        return "↑"

    if direction(row) == "DOWN":
        return "↓"

    return "-"


def probability_strength(row):
    if row is None:
        return 0.0

    return max(
        float(row["p_up"]),
        float(row["p_down"]),
    )


def result_symbol(row):
    if row["actual_direction"] is None:
        return "…"

    if row["correct"] == 1:
        return "✓"

    if row["correct"] == 0:
        return "✗"

    return "-"


def stat_text(value, kind):
    if value is None:
        return "-"

    if kind == "percent":
        return f"{value * 100:.1f}%"

    if kind == "brier":
        return f"{value:.4f}"

    return str(value)


def phase_text(row):
    if row is None:
        return "-"

    phase = row["phase"]

    if phase == "phase1":
        return "Phase 1"

    if phase == "phase2":
        return "Phase 2"

    if phase == "phase3":
        return "Phase 3"

    return html.escape(phase or "-")


def prediction_card(row, title):
    if row is None:
        return """
        <div class="predictor-card">
            <h3>Unavailable</h3>
        </div>
        """

    p_up = float(row["p_up"])
    p_down = float(row["p_down"])

    predicted = direction(row)
    strength = probability_strength(row)

    model_version = html.escape(
        row["model_version"]
        or row["model"]
        or "-"
    )

    reason = html.escape(
        row["reason"] or "-"
    )

    confidence = row["confidence"]

    if confidence is None:
        confidence_text = "—"
    else:
        confidence_text = (
            f"{float(confidence) * 100:.1f}%"
        )

    confidence_html = ""
    if row["predictor"] == "jev":
        confidence_html = (
            '<div class="confidence">'
            f'Jev confidence: {confidence_text}</div>'
        )

    predicted_class = (
        "up-text"
        if predicted == "UP"
        else "down-text"
    )

    return f"""
    <div class="predictor-card">

        <div class="predictor-name">
            {html.escape(title)}
        </div>

        <div class="model-version">
            {model_version}
        </div>

        <div class="big-prediction {predicted_class}">
            {direction_arrow(row)} {predicted}
        </div>

        <div class="big-percent">
            {strength * 100:.1f}%
        </div>

        <div class="prob-row">
            <span>UP</span>
            <div class="mini-bar">
                <div
                    class="mini-up"
                    style="width:{p_up * 100:.1f}%">
                </div>
            </div>
            <strong>{p_up * 100:.1f}%</strong>
        </div>

        <div class="prob-row">
            <span>DOWN</span>
            <div class="mini-bar">
                <div
                    class="mini-down"
                    style="width:{p_down * 100:.1f}%">
                </div>
            </div>
            <strong>{p_down * 100:.1f}%</strong>
        </div>

        {confidence_html}

        <div class="reason">
            {reason}
        </div>

    </div>
    """


def scoreboard_html(
    title,
    subtitle,
    gpt_stats,
    jev_stats,
):
    return f"""
    <div class="card">

    <h2>{html.escape(title)}</h2>

    <div class="section-subtitle">
        {html.escape(subtitle)}
    </div>

    <div class="scoreboard">

    <div class="score">

    <div class="score-title">
    GPT
    </div>

    <div class="score-grid">

    <div>
    <div class="stat-value">
    {gpt_stats["wins"]}/{gpt_stats["n"]}
    </div>
    <div class="stat-label">
    Correct
    </div>
    </div>

    <div>
    <div class="stat-value">
    {stat_text(
        gpt_stats["accuracy"],
        "percent"
    )}
    </div>
    <div class="stat-label">
    Accuracy
    </div>
    </div>

    <div>
    <div class="stat-value">
    {stat_text(
        gpt_stats["brier"],
        "brier"
    )}
    </div>
    <div class="stat-label">
    Brier score
    </div>
    </div>

    <div>
    <div class="stat-value">
    {stat_text(
        gpt_stats["strength"],
        "percent"
    )}
    </div>
    <div class="stat-label">
    Avg prediction strength
    </div>
    </div>

    </div>
    </div>

    <div class="score">

    <div class="score-title">
    Jev
    </div>

    <div class="score-grid">

    <div>
    <div class="stat-value">
    {jev_stats["wins"]}/{jev_stats["n"]}
    </div>
    <div class="stat-label">
    Correct
    </div>
    </div>

    <div>
    <div class="stat-value">
    {stat_text(
        jev_stats["accuracy"],
        "percent"
    )}
    </div>
    <div class="stat-label">
    Accuracy
    </div>
    </div>

    <div>
    <div class="stat-value">
    {stat_text(
        jev_stats["brier"],
        "brier"
    )}
    </div>
    <div class="stat-label">
    Brier score
    </div>
    </div>

    <div>
    <div class="stat-value">
    {stat_text(
        jev_stats["strength"],
        "percent"
    )}
    </div>
    <div class="stat-label">
    Avg prediction strength
    </div>
    </div>

    </div>
    </div>

    </div>
    </div>
    """


def get_latest_microstructure():
    """Recompute windows at the latest Phase 3 prediction cutoff, read-only.

    candle_time is the START of the reference hourly candle, so the
    prediction cutoff is candle_time + 3600. Windows match microstructure.py:
    (cutoff - minutes * 60, cutoff]. This is a database reconstruction,
    not necessarily the exact stored input if trades arrived late.
    """
    result = {"cutoff": None, "windows": [], "status": ""}
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(candle_time) AS candle_time FROM predictions "
            "WHERE phase = ? AND predictor IN ('openai', 'jev')",
            (CURRENT_PHASE,),
        ).fetchone()
    if row["candle_time"] is None:
        result["status"] = "No Phase 3 predictions yet."
        return result

    cutoff = int(row["candle_time"]) + 3600
    result["cutoff"] = cutoff
    conn = None
    try:
        # mode=ro prevents accidental creation of an empty market.db.
        conn = sqlite3.connect(
            MARKET_DB_PATH.resolve().as_uri() + "?mode=ro",
            uri=True, timeout=2,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        for minutes in (1, 5, 15, 60):
            bounds = (cutoff - minutes * 60, cutoff)
            book = conn.execute("""
                SELECT COUNT(*) AS samples,
                       AVG(top5_imbalance) AS imb5,
                       AVG(top10_imbalance) AS imb10,
                       AVG(top20_imbalance) AS imb20
                FROM orderbook_samples
                WHERE timestamp > ? AND timestamp <= ?
            """, bounds).fetchone()
            flow = conn.execute("""
                SELECT COUNT(*) AS trades,
                       SUM(CASE WHEN aggressor_side = 'buy'
                                THEN size ELSE 0 END) AS buy_btc,
                       SUM(CASE WHEN aggressor_side = 'sell'
                                THEN size ELSE 0 END) AS sell_btc
                FROM trades
                WHERE timestamp > ? AND timestamp <= ?
            """, bounds).fetchone()
            total = (flow["buy_btc"] or 0) + (flow["sell_btc"] or 0)
            ratio = None
            if flow["trades"]:
                ratio = flow["buy_btc"] / total if total > 0 else 0.5
            result["windows"].append({
                "minutes": minutes, **dict(book),
                "trades": flow["trades"], "buy_ratio": ratio,
            })
    except sqlite3.Error:
        app.logger.warning("Market microstructure unavailable", exc_info=True)
        result["windows"] = []
        result["status"] = "Market data unavailable."
    finally:
        if conn is not None:
            conn.close()
    return result


def microstructure_html(data):
    rows = ""
    for window in data["windows"]:
        imbalances = "".join(
            f"<td>{window[key]:.3f}</td>"
            if window[key] is not None else "<td>—</td>"
            for key in ("imb5", "imb10", "imb20")
        )
        ratio = window["buy_ratio"]
        ratio_text = f"{ratio * 100:.1f}%" if ratio is not None else "—"
        rows += (
            f'<tr><td>{window["minutes"]}m</td>{imbalances}'
            f'<td>{ratio_text}</td><td>{window["samples"]}</td>'
            f'<td>{window["trades"]}</td></tr>'
        )
    status = html.escape(data["status"])
    content = f'<div class="stat-label">{status}</div>' if status else f"""
        <div class="table-scroll">
        <table>
        <tr><th>Window</th><th>Top 5</th><th>Top 10</th><th>Top 20</th>
        <th>Buy ratio</th><th>Samples</th><th>Trades</th></tr>
        {rows}
        </table>
        </div>
    """
    return f"""
    <div class="card">
        <h2>Latest Market Microstructure</h2>
        <div class="section-subtitle">
            Phase 3 prediction cutoff: {format_time(data["cutoff"])}<br>
            Order-book imbalance (window average) / Trade-flow buy ratio (BTC volume)
        </div>
        {content}
        <div class="stat-label spaced">
            Reconstructed from market.db at the cutoff; late-arriving trades
            may differ from the stored prediction input. — means no data.
        </div>
    </div>
    """


@app.route("/direction/")
def index():

    try:
        current_price = get_btc_price()
        price_text = f"${current_price:,.2f}"

    except Exception:
        price_text = "Unavailable"

    gpt, jev = get_latest_battle()

    #
    # Phase 3: current experiment
    #

    gpt_stats_phase3 = get_model_statistics("openai", CURRENT_PHASE)
    jev_stats_phase3 = get_model_statistics("jev", CURRENT_PHASE)
    microstructure = microstructure_html(get_latest_microstructure())

    # Phase 2: completed experiment
    gpt_stats_phase2 = get_model_statistics(
        "openai",
        "phase2",
    )

    jev_stats_phase2 = get_model_statistics(
        "jev",
        "phase2",
    )

    gpt_trading = get_virtual_trading(
        "openai",
        CURRENT_PHASE,
    )

    jev_trading = get_virtual_trading(
        "jev",
        CURRENT_PHASE,
    )

    #
    # Phase 1: completed experiment
    #

    gpt_stats_phase1 = get_model_statistics(
        "openai",
        "phase1",
    )

    jev_stats_phase1 = get_model_statistics(
        "jev",
        "phase1",
    )

    battles = get_recent_battles(
        limit=12
    )

    if gpt:
        battle_time = format_time(
            gpt["candle_time"]
        )

        battle_phase = phase_text(gpt)

        actual = (
            gpt["actual_direction"]
            or "Waiting"
        )

    else:
        battle_time = "-"
        battle_phase = "-"
        actual = "Waiting"

    recent_rows = ""

    for battle_gpt, battle_jev in battles:

        candle_time = battle_gpt[
            "candle_time"
        ]

        gpt_strength = (
            probability_strength(
                battle_gpt
            )
        )

        jev_strength = (
            probability_strength(
                battle_jev
            )
        )

        actual_direction = (
            battle_gpt["actual_direction"]
            or "-"
        )

        actual_return = (
            battle_gpt["actual_return"]
        )

        if actual_return is None:
            actual_text = "-"
        else:
            actual_text = (
                f"{actual_direction} "
                f"{float(actual_return):+.2f}%"
            )

        recent_rows += f"""
        <tr>
            <td>
                {format_time(candle_time)}
            </td>

            <td>
                {phase_text(battle_gpt)}
            </td>

            <td>
                <span class="prediction-inline">
                    {direction_arrow(battle_gpt)}
                    {gpt_strength * 100:.0f}%
                </span>
                {result_symbol(battle_gpt)}
            </td>

            <td>
                <span class="prediction-inline">
                    {direction_arrow(battle_jev)}
                    {jev_strength * 100:.0f}%
                </span>
                {result_symbol(battle_jev)}
            </td>

            <td>
                {actual_text}
            </td>
        </tr>
        """

    phase3_scoreboard = scoreboard_html(
        "Phase 3 Scoreboard",
        PHASE3_SUBTITLE,
        gpt_stats_phase3,
        jev_stats_phase3,
    )

    phase2_scoreboard = scoreboard_html(
        "Phase 2 Final Results",
        "Multi-timeframe input: 1h + 15m + 5m",
        gpt_stats_phase2,
        jev_stats_phase2,
    )

    phase1_scoreboard = scoreboard_html(
        "Phase 1 Final Results",
        "Completed experiment: 1-hour indicators only",
        gpt_stats_phase1,
        jev_stats_phase1,
    )

    return page_shell("Direction — Phase 1–3", f"""

<h1>Direction — Phase 1–3</h1>


<div class="subtitle">
GPT vs Jev —
BTC-USD 1-hour probability experiment
</div>


<div class="card">

<div>Current BTC-USD</div>

<div class="price">
{price_text}
</div>

</div>


<div class="card">

<div class="battle-header">

<div>
<h2>Latest Battle</h2>
<div>{battle_time}</div>
<div class="phase-badge">
{battle_phase}
</div>
</div>

<div class="actual">
Actual: {actual}
</div>

</div>


<div class="battle">

{prediction_card(
    gpt,
    "GPT"
)}

{prediction_card(
    jev,
    "Jev"
)}

</div>

</div>


{phase3_scoreboard}

{microstructure}

<div class="card">

<h2>Phase 3 Virtual Trading</h2>

<div class="section-subtitle">
{PHASE3_SUBTITLE}
</div>

<div class="scoreboard">

<div class="score">

<div class="score-title">
GPT
</div>

<div class="stat-value">
¥{gpt_trading["balance"]:,.0f}
</div>

<div class="stat-label">
{gpt_trading["return"]:+.2f}% /
{gpt_trading["trades"]} trades
</div>

</div>


<div class="score">

<div class="score-title">
Jev
</div>

<div class="stat-value">
¥{jev_trading["balance"]:,.0f}
</div>

<div class="stat-label">
{jev_trading["return"]:+.2f}% /
{jev_trading["trades"]} trades
</div>

</div>

</div>

<div class="stat-label"
     style="margin-top: 14px;">
Initial balance: ¥10,000 /
Position size = 2 × P(UP) − 1 /
No fees or spread
</div>

</div>


{phase2_scoreboard}

{phase1_scoreboard}


<div class="card">

<h2>Recent Battles</h2>

<div class="table-scroll"><table>

<tr>
<th>Time</th>
<th>Phase</th>
<th>GPT</th>
<th>Jev</th>
<th>Actual</th>
</tr>

{recent_rows}

</table></div>

</div>


<div class="footer">

BTC-USD market data: Coinbase<br>

Phase 1: 1-hour technical indicators only.<br>

Phase 2: multi-timeframe input
(1h + 15m + 5m).<br>

Phase 3: 1h + 15m + 5m + order book + trade flow.<br>

Predictions use only the stored market snapshot
available at prediction time.<br>

Experimental probability forecasting.
Not financial advice.

</div>

""", "direction", refresh=300)


# Analysis uses the stored 0..1 probability scale, matching the dashboard.
ANALYSIS_PHASES = ("phase1", "phase2", "phase3")
ANALYSIS_MODELS = (("openai", "GPT"), ("jev", "Jev"))
ANALYSIS_BUCKETS = ((.50, .55), (.55, .60), (.60, .70),
                    (.70, .80), (.80, .90), (.90, 1.0))


def analysis_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 1 else None


def analysis_summary(rows):
    n = len(rows)
    correct = sum(row["correct"] for row in rows)
    return {
        "n": n, "correct": correct,
        "accuracy": correct / n if n else None,
        "brier": math.fsum(row["brier"] for row in rows) / n if n else None,
        "strength": math.fsum(row["strength"] for row in rows) / n if n else None,
    }


def get_analysis(phase):
    # One read-only snapshot per request; never create or modify btc.db.
    conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT phase, predictor, p_up, p_down, actual_direction, correct,
                   confidence
            FROM predictions
            WHERE actual_direction IN ('UP', 'DOWN')
              AND predictor IN ('openai', 'jev')
              AND phase IN ('phase1', 'phase2', 'phase3')
        """
        params = ()
        if phase != "all":
            sql += " AND phase = ?"
            params = (phase,)
        raw = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    rows, skipped, fallback_correct = [], 0, 0
    for row in raw:
        up, down = analysis_number(row["p_up"]), analysis_number(row["p_down"])
        if up is None or down is None or not math.isclose(up + down, 1.0, abs_tol=1e-6):
            skipped += 1
            continue
        correct = row["correct"]
        if correct not in (0, 1):
            # Same tie rule as direction(): UP when probabilities are equal.
            correct = int(("UP" if up >= down else "DOWN") == row["actual_direction"])
            fallback_correct += 1
        rows.append({
            "phase": row["phase"], "predictor": row["predictor"],
            "correct": int(correct), "strength": max(up, down),
            "brier": (up - int(row["actual_direction"] == "UP")) ** 2,
            "margin": abs(up - down),
            "confidence": analysis_number(row["confidence"]),
        })

    phases = ANALYSIS_PHASES if phase == "all" else (phase,)
    comparison = [
        (p, label, analysis_summary([r for r in rows if r["phase"] == p and r["predictor"] == model]))
        for p in phases for model, label in ANALYSIS_MODELS
    ]
    calibration, thresholds = {}, {}
    for model, label in ANALYSIS_MODELS:
        selected = [r for r in rows if r["predictor"] == model]
        calibration[label] = [
            (lo, hi, analysis_summary([
                r for r in selected if lo <= r["strength"]
                and (r["strength"] < hi or hi == 1.0)
            ])) for lo, hi in ANALYSIS_BUCKETS
        ]
        thresholds[label] = [
            (threshold, analysis_summary([r for r in selected if r["strength"] >= threshold]))
            for threshold in (.50, .55, .60, .70, .80, .90)
        ]

    jev = [r for r in rows if r["predictor"] == "jev"]
    pairs = [r for r in jev if r["confidence"] is not None]
    n = len(pairs)
    correlation, mad = None, None
    if n:
        mx = math.fsum(r["margin"] for r in pairs) / n
        my = math.fsum(r["confidence"] for r in pairs) / n
        mad = math.fsum(abs(r["margin"] - r["confidence"]) for r in pairs) / n
        xx = math.fsum((r["margin"] - mx) ** 2 for r in pairs)
        yy = math.fsum((r["confidence"] - my) ** 2 for r in pairs)
        if n >= 2 and xx > 0 and yy > 0:
            xy = math.fsum((r["margin"] - mx) * (r["confidence"] - my) for r in pairs)
            correlation = max(-1.0, min(1.0, xy / math.sqrt(xx * yy)))
    margin_groups = []
    for i in range(10):
        group = [r for r in pairs if i / 10 <= r["margin"]
                 and (r["margin"] < (i + 1) / 10 or i == 9)]
        size = len(group)
        margin_groups.append((i, size,
            math.fsum(r["margin"] for r in group) / size if size else None,
            math.fsum(r["confidence"] for r in group) / size if size else None))
    return dict(comparison=comparison, calibration=calibration, thresholds=thresholds,
                pair_n=n, correlation=correlation, mad=mad, margin_groups=margin_groups,
                missing_confidence=len(jev) - n, skipped=skipped,
                fallback_correct=fallback_correct)


def analysis_table(headers, rows):
    head = "".join(f'<th scope="col">{html.escape(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def analysis_bar(stats):
    if not stats["n"]:
        return "—"
    actual = stats["accuracy"] * 100
    ideal = stats["strength"] * 100
    return (f'<div class="bar" role="img" aria-label="Accuracy {actual:.1f}%, '
            f'ideal {ideal:.1f}%"><span style="width:{actual:.4f}%"></span>'
            f'<i style="left:{ideal:.4f}%"></i></div>')


@app.route("/analyze/")
def analyze():
    if request.args.get("phase") in ("phase4r", "phase5"):
        from next_stage_views import page
        return page(analysis=True, section="regression" if request.args.get("phase") == "phase5" else "repeated")
    if request.args.get("phase") == "phase4":
        from volatility_views import page
        response = app.make_response(page(analysis=True))
        response.headers["Cache-Control"] = "no-store"
        return response
    phase = request.args.get("phase", "all")
    if phase not in ("all",) + ANALYSIS_PHASES:
        abort(400, description="phase must be all, phase1, phase2 or phase3")
    data = get_analysis(phase)
    comparison = analysis_table(
        ["Phase", "Model", "N", "Correct", "Accuracy", "Brier score", "Avg prediction strength"],
        [(f'Phase {p[-1]}', label, s["n"], s["correct"], stat_text(s["accuracy"], "percent"),
          stat_text(s["brier"], "brier"), stat_text(s["strength"], "percent"))
         for p, label, s in data["comparison"]])
    calibration, thresholds = "", ""
    for _, label in ANALYSIS_MODELS:
        calibration += f"<h3>{label}</h3>" + analysis_table(
            ["Strength bucket", "N", "Accuracy", "Ideal (mean strength)", "Actual / ideal · 0–100%"],
            [(f'{lo:.0%}–{hi:.0%}', s["n"], stat_text(s["accuracy"], "percent"),
              stat_text(s["strength"], "percent"), analysis_bar(s))
             for lo, hi, s in data["calibration"][label]])
        thresholds += f"<h3>{label}</h3>" + analysis_table(
            ["Minimum strength", "N", "Accuracy", "Brier score"],
            [(f'≥ {threshold:.0%}', s["n"], stat_text(s["accuracy"], "percent"),
              stat_text(s["brier"], "brier")) for threshold, s in data["thresholds"][label]])
    confidence_table = analysis_table(
        ["Probability margin", "N", "Mean margin", "Mean Jev confidence"],
        [(f'{i * 10}–{(i + 1) * 10}%', n, stat_text(margin, "percent"), stat_text(conf, "percent"))
         for i, n, margin, conf in data["margin_groups"]])
    r_text = f'{data["correlation"]:.6f}' if data["correlation"] is not None else "— (fewer than 2 pairs or zero variance)"
    mad_text = f'{data["mad"] * 100:.3f} percentage points' if data["mad"] is not None else "—"
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    page = page_shell("Analysis — Direction", f"""<h1>Analysis — Direction</h1><p class="muted">Recomputed from btc.db on every request · {updated}</p>

<p class="muted">The phase filter applies to all sections. Only evaluated UP/DOWN rows are included.
Strength = max(p_up, p_down). Accuracy uses stored correct (missing values are recomputed; ties predict UP).
Brier = mean((p_up − outcome)²), where UP = 1 and DOWN = 0; lower is better (50/50 baseline: 0.25).
Empty groups show —. Invalid probability rows excluded: {data['skipped']}; correct values recomputed: {data['fallback_correct']}.</p>
<section><h2>Phase comparison</h2>{comparison}</section>
<section><h2>Calibration analysis</h2><p class="muted">Blue bar: actual accuracy. Black marker: ideal accuracy, equal to the mean prediction strength in that bucket.
Buckets include their lower bound and exclude their upper bound, except 100% is included in the last bucket.</p>{calibration}</section>
<section><h2>Threshold analysis</h2><p class="muted">Keep predictions whose strength is at least the threshold (not Jev's separate confidence field).
Compare accuracy and Brier together with N; higher thresholds leave fewer observations.
These are historical subsets, not evidence of future trading profit.</p>{thresholds}</section>
<section><h2>Jev confidence investigation</h2>
<p class="muted">Probability margin = abs(p_up − p_down). Evaluated Jev rows with valid confidence only.
Missing or invalid confidence excluded: {data['missing_confidence']}.</p>
<div class="metrics"><div>Paired N<strong>{data['pair_n']}</strong></div><div>Pearson r<strong>{r_text}</strong></div>
<div>Mean absolute difference<strong>{mad_text}</strong></div></div>
<p class="muted">If confidence repeats the probability margin, r approaches 1 and the absolute difference approaches 0.
The table groups pairs by margin (lower bound included; 100% included in the last group).</p>{confidence_table}</section>
""", "analysis", phase=phase)
    response = app.make_response(page)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/next-stages/")
def next_stages_dashboard():
    from next_stage_views import page
    return page()

@app.route("/next-stages/data.json")
def next_stages_data():
    from next_stage_views import report
    return report()

@app.route("/")
@app.route("/volatility/")
def volatility_dashboard():
    from volatility_views import page
    response = app.make_response(page(active="current" if request.path == "/" else "volatility"))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/repeated/")
def repeated_dashboard():
    from repeat_views import page
    return page()

@app.route("/regression/")
def regression_dashboard():
    from regression_views import page
    return page()

@app.after_request
def read_only_page_cache(response):
    if request.path != '/static/style.css':
        response.headers['Cache-Control'] = 'no-store'
    return response


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=8001,
    )
