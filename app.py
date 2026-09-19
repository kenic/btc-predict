import html
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import requests
from flask import Flask


app = Flask(__name__)

DB_PATH = Path(__file__).parent / "btc.db"

COINBASE_URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/ticker"
)

HEADERS = {
    "User-Agent": "btc-predict-dashboard/0.2"
}


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


def get_model_statistics(predictor):
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                p_up,
                p_down,
                actual_direction,
                correct
            FROM predictions
            WHERE predictor = ?
              AND actual_direction IN ('UP', 'DOWN')
        """, (
            predictor,
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

        <div class="confidence">
            Jev confidence:
            {confidence_text}
        </div>

        <div class="reason">
            {reason}
        </div>

    </div>
    """


@app.route("/")
def index():

    try:
        current_price = get_btc_price()
        price_text = f"${current_price:,.2f}"

    except Exception:
        price_text = "Unavailable"

    gpt, jev = get_latest_battle()

    gpt_stats = get_model_statistics(
        "openai"
    )

    jev_stats = get_model_statistics(
        "jev"
    )

    battles = get_recent_battles(
        limit=12
    )

    if gpt:
        battle_time = format_time(
            gpt["candle_time"]
        )

        actual = (
            gpt["actual_direction"]
            or "Waiting"
        )

    else:
        battle_time = "-"
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

    return f"""
<!doctype html>
<html>

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1"
>

<meta
    http-equiv="refresh"
    content="300"
>

<link
    rel="icon"
    type="image/png"
    href="/static/btc.png"
>

<link
    rel="apple-touch-icon"
    href="/static/btc.png"
>

<title>BTC Predictor</title>

<style>

* {{
    box-sizing: border-box;
}}

body {{
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

    background: #f5f5f7;

    margin: 0;
    padding: 30px;

    color: #222;
}}

.container {{
    max-width: 1050px;
    margin: auto;
}}

h1 {{
    margin-bottom: 4px;
}}

h2 {{
    margin-top: 0;
}}

.subtitle {{
    color: #777;
    margin-bottom: 28px;
}}

.card {{
    background: white;

    border-radius: 16px;

    padding: 24px;

    margin-bottom: 22px;

    box-shadow:
        0 2px 12px
        rgba(0, 0, 0, 0.06);
}}

.price {{
    font-size: 44px;
    font-weight: 700;
    margin-top: 5px;
}}

.battle-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 22px;
}}

.actual {{
    font-weight: 600;
}}

.battle {{
    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 20px;
}}

.predictor-card {{
    background: #fafafa;

    border: 1px solid #eee;

    border-radius: 14px;

    padding: 22px;
}}

.predictor-name {{
    font-size: 22px;
    font-weight: 700;
}}

.model-version {{
    color: #888;
    font-size: 13px;
    margin-top: 3px;
}}

.big-prediction {{
    font-size: 36px;
    font-weight: 700;
    margin-top: 22px;
}}

.big-percent {{
    font-size: 28px;
    font-weight: 600;
    margin-bottom: 20px;
}}

.up-text {{
    color: #258a43;
}}

.down-text {{
    color: #c33b32;
}}

.prob-row {{
    display: grid;

    grid-template-columns:
        55px 1fr 55px;

    align-items: center;

    gap: 10px;

    margin-top: 10px;
}}

.mini-bar {{
    background: #e7e7e7;
    height: 12px;
    border-radius: 8px;
    overflow: hidden;
}}

.mini-up {{
    height: 100%;
    background: #4caf50;
}}

.mini-down {{
    height: 100%;
    background: #e74c3c;
}}

.confidence {{
    margin-top: 20px;
    font-size: 14px;
    color: #666;
}}

.reason {{
    margin-top: 16px;

    padding-top: 16px;

    border-top: 1px solid #e5e5e5;

    color: #555;

    font-size: 14px;
    line-height: 1.6;
}}

.scoreboard {{
    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 20px;
}}

.score {{
    background: #fafafa;
    border-radius: 12px;
    padding: 18px;
}}

.score-title {{
    font-size: 20px;
    font-weight: 700;
    margin-bottom: 15px;
}}

.score-grid {{
    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 14px;
}}

.stat-value {{
    font-size: 25px;
    font-weight: 700;
}}

.stat-label {{
    color: #888;
    font-size: 12px;
}}

table {{
    width: 100%;
    border-collapse: collapse;
}}

th {{
    text-align: left;

    padding: 11px;

    border-bottom:
        2px solid #ddd;
}}

td {{
    padding: 11px;

    border-bottom:
        1px solid #eee;
}}

.prediction-inline {{
    font-weight: 600;
    margin-right: 8px;
}}

.footer {{
    text-align: center;

    color: #999;

    margin-top: 28px;

    font-size: 12px;

    line-height: 1.6;
}}

@media
(max-width: 700px) {{

    body {{
        padding: 14px;
    }}

    .battle {{
        grid-template-columns: 1fr;
    }}

    .scoreboard {{
        grid-template-columns: 1fr;
    }}

    .battle-header {{
        display: block;
    }}

    .actual {{
        margin-top: 8px;
    }}

    table {{
        font-size: 12px;
    }}

    .price {{
        font-size: 36px;
    }}
}}

</style>

</head>

<body>

<div class="container">

<h1>BTC Predictor</h1>

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


<div class="card">

<h2>Scoreboard</h2>

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


<div class="card">

<h2>Recent Battles</h2>

<table>

<tr>
<th>Time</th>
<th>GPT</th>
<th>Jev</th>
<th>Actual</th>
</tr>

{recent_rows}

</table>

</div>


<div class="footer">

BTC-USD market data: Coinbase<br>

Predictions use only the stored market snapshot
available at prediction time.<br>

Experimental probability forecasting.
Not financial advice.

</div>

</div>

</body>

</html>
"""


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=8001,
    )
