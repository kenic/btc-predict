import sqlite3
import requests

from datetime import datetime, timezone
from pathlib import Path

from flask import Flask


app = Flask(__name__)

DB_PATH = Path(__file__).parent / "btc.db"

COINBASE_URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/ticker"
)

HEADERS = {
    "User-Agent": "btc-predict-dashboard/0.1"
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

    return float(
        r.json()["price"]
    )


def get_latest_prediction():
    with connect() as conn:
        row = conn.execute("""
            SELECT *
            FROM predictions
            ORDER BY candle_time DESC
            LIMIT 1
        """).fetchone()

    return row


def get_recent_predictions(limit=10):
    with connect() as conn:
        rows = conn.execute("""
            SELECT *
            FROM predictions
            ORDER BY candle_time DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return rows


def get_statistics():
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                p_up,
                p_down,
                actual_direction,
                correct
            FROM predictions
            WHERE actual_direction IN ('UP', 'DOWN')
        """).fetchall()

    n = len(rows)

    if n == 0:
        return {
            "n": 0,
            "accuracy": None,
            "brier": None,
            "confidence": None,
        }

    hits = 0
    brier_sum = 0.0
    confidence_sum = 0.0

    for row in rows:
        p_up = float(
            row["p_up"]
        )

        p_down = float(
            row["p_down"]
        )

        if row["actual_direction"] == "UP":
            outcome = 1.0
        else:
            outcome = 0.0

        brier_sum += (
            p_up - outcome
        ) ** 2

        confidence_sum += max(
            p_up,
            p_down,
        )

        if row["correct"] == 1:
            hits += 1

    return {
        "n": n,
        "accuracy": hits / n,
        "brier": brier_sum / n,
        "confidence": confidence_sum / n,
    }


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


def result_symbol(row):
    if row["actual_direction"] is None:
        return "Waiting"

    if row["correct"] == 1:
        return "✓"

    if row["correct"] == 0:
        return "✗"

    return "-"


@app.route("/")
def index():

    # ----------------------------
    # Current BTC price
    # ----------------------------

    try:
        current_price = get_btc_price()

        price_text = (
            f"${current_price:,.2f}"
        )

    except Exception:
        price_text = "Unavailable"

    # ----------------------------
    # Database
    # ----------------------------

    latest = get_latest_prediction()

    recent = get_recent_predictions(
        limit=10
    )

    stats = get_statistics()

    # ----------------------------
    # Latest prediction
    # ----------------------------

    if latest:

        p_up = float(
            latest["p_up"]
        )

        p_down = float(
            latest["p_down"]
        )

        up_percent = p_up * 100
        down_percent = p_down * 100

        prediction = (
            "UP"
            if p_up >= p_down
            else "DOWN"
        )

        latest_time = format_time(
            latest["candle_time"]
        )

        latest_model = latest["model"]

        latest_reason = (
            latest["reason"] or "-"
        )

    else:

        up_percent = 50
        down_percent = 50

        prediction = "-"

        latest_time = "-"

        latest_model = "-"

        latest_reason = (
            "No prediction yet."
        )

    # ----------------------------
    # Statistics
    # ----------------------------

    if stats["accuracy"] is None:

        accuracy_text = "-"

        brier_text = "-"

        confidence_text = "-"

    else:

        accuracy_text = (
            f"{stats['accuracy'] * 100:.1f}%"
        )

        brier_text = (
            f"{stats['brier']:.4f}"
        )

        confidence_text = (
            f"{stats['confidence'] * 100:.1f}%"
        )

    # ----------------------------
    # Recent prediction rows
    # ----------------------------

    table_rows = ""

    for row in recent:

        p_up = float(
            row["p_up"]
        )

        p_down = float(
            row["p_down"]
        )

        predicted = (
            "UP"
            if p_up >= p_down
            else "DOWN"
        )

        actual = (
            row["actual_direction"]
            or "-"
        )

        result = result_symbol(
            row
        )

        table_rows += f"""
        <tr>
            <td>
                {format_time(row["candle_time"])}
            </td>

            <td>
                {row["model"]}
            </td>

            <td>
                {p_up * 100:.1f}%
            </td>

            <td>
                {p_down * 100:.1f}%
            </td>

            <td>
                {predicted}
            </td>

            <td>
                {actual}
            </td>

            <td>
                {result}
            </td>
        </tr>
        """

    # ----------------------------
    # HTML
    # ----------------------------

    return f"""
<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1"
>

<title>BTC Predictor</title>

<style>

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
    max-width: 1000px;
    margin: auto;
}}

h1 {{
    margin-bottom: 5px;
}}

.subtitle {{
    color: #777;
    margin-bottom: 30px;
}}

.card {{
    background: white;

    border-radius: 14px;

    padding: 24px;

    margin-bottom: 20px;

    box-shadow:
        0 2px 10px
        rgba(0,0,0,0.06);
}}

.price {{
    font-size: 42px;
    font-weight: 600;
}}

.prediction {{
    font-size: 36px;
    font-weight: 600;

    margin-top: 10px;
}}

.bar {{
    display: flex;

    width: 100%;

    height: 36px;

    border-radius: 8px;

    overflow: hidden;

    margin-top: 20px;
}}

.up {{
    width: {up_percent}%;

    background: #4caf50;

    color: white;

    display: flex;

    align-items: center;

    justify-content: center;
}}

.down {{
    width: {down_percent}%;

    background: #e74c3c;

    color: white;

    display: flex;

    align-items: center;

    justify-content: center;
}}

.stats {{
    display: grid;

    grid-template-columns:
        repeat(4, 1fr);

    gap: 15px;
}}

.stat {{
    background: #fafafa;

    padding: 15px;

    border-radius: 10px;
}}

.stat-value {{
    font-size: 26px;
    font-weight: 600;
}}

.stat-label {{
    color: #777;
    font-size: 14px;
}}

.reason {{
    line-height: 1.6;

    color: #444;

    margin-top: 20px;
}}

table {{
    width: 100%;

    border-collapse: collapse;

    font-size: 14px;
}}

th {{
    text-align: left;

    padding: 10px;

    border-bottom:
        2px solid #ddd;
}}

td {{
    padding: 10px;

    border-bottom:
        1px solid #eee;
}}

.footer {{
    text-align: center;

    color: #999;

    margin-top: 30px;

    font-size: 13px;
}}

@media
(max-width: 700px) {{

    body {{
        padding: 15px;
    }}

    .stats {{
        grid-template-columns:
            repeat(2, 1fr);
    }}

    table {{
        font-size: 11px;
    }}

}}

</style>

</head>

<body>

<div class="container">

<h1>BTC Predictor</h1>

<div class="subtitle">
AI BTC-USD 1-hour direction experiment
</div>


<div class="card">

<div>
Current BTC-USD
</div>

<div class="price">
{price_text}
</div>

</div>


<div class="card">

<h2>Latest prediction</h2>

<div>
{latest_time}
</div>

<div>
Model: {latest_model}
</div>

<div class="prediction">
{prediction}
</div>

<div class="bar">

<div class="up">
UP {up_percent:.1f}%
</div>

<div class="down">
DOWN {down_percent:.1f}%
</div>

</div>

<div class="reason">
<strong>Reason</strong><br>
{latest_reason}
</div>

</div>


<div class="card">

<h2>Performance</h2>

<div class="stats">

<div class="stat">

<div class="stat-value">
{stats["n"]}
</div>

<div class="stat-label">
Evaluated predictions
</div>

</div>


<div class="stat">

<div class="stat-value">
{accuracy_text}
</div>

<div class="stat-label">
Accuracy
</div>

</div>


<div class="stat">

<div class="stat-value">
{brier_text}
</div>

<div class="stat-label">
Brier score
</div>

</div>


<div class="stat">

<div class="stat-value">
{confidence_text}
</div>

<div class="stat-label">
Average confidence
</div>

</div>

</div>

</div>


<div class="card">

<h2>Recent predictions</h2>

<table>

<tr>
<th>Time</th>
<th>Model</th>
<th>UP</th>
<th>DOWN</th>
<th>Prediction</th>
<th>Actual</th>
<th>Result</th>
</tr>

{table_rows}

</table>

</div>


<div class="footer">

BTC-USD market data: Coinbase<br>

Predictions are experimental and
are not financial advice.

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
