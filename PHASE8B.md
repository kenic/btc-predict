# Phase 8b — technical indicators plus identical Phase 8a news

Production: https://btc.kenic.jp/phase8b/ (JSON: `/phase8b/data.json`).
Phase 8a remains at `/phase8a/`, unchanged. Five assets: BTC/ETH/SOL/XRP/LINK,
four existing providers, Kairos `general`, 4h target, paper trading only.

## Experimental contract

8b loads the immutable 8a `runs/<cutoff>/manifest.json`, verifies its plan hash,
and requires the same settings (except the schema name). It preserves the exact
news collection and every article version. If 8a has no frozen manifest, or any
asset has missing indicators, the entire 8b batch is skipped without model calls.
8a never depends on 8b. 8a dispatches at hour+10s and 8b at hour+30s; the information
cutoff and target times are identical, but the actual dispatch times differ and
are recorded. Both have the existing +60s start/+120s completion limits.

8b is a frozen copy of the 8a execution/evaluation/provider code, with its own
package, schema, SQLite application ID, source hashes, ledgers, and output root.
This intentionally avoids altering the running 8a code fingerprint. Future code
changes require a new versioned output directory; never reset an existing ledger.
The duplicated execution code should only be consolidated in a new experiment
version, with regression tests. Current APIs/model settings are reused unchanged.

## Point-in-time technical inputs

Dedicated collector: public Coinbase Exchange USD product candles, 300s bars,
220 bars per asset once an hour at UTC :56:15; initial collection at deployment.
Official reference: https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles
The endpoint permits 300 candles and may omit intervals without trades or return
rows outside the requested range. Filter those rows; never interpolate missing bars.
No authenticated market or order endpoints are used.

Each first observation is immutable with acquisition timestamp. Historical candles
fetched at startup are available only from that timestamp, never backdated.
A forecast requires exactly 200 consecutive bars; the last ends at cutoff-300s.
Every bar must have been acquired by cutoff, and closed for at least 60s when
acquired. This fixed 5-minute lag allows collection before the prediction cutoff.
Full raw candle arrays, acquisition times, per-candle SHA256 IDs, snapshot ID,
feature version, news IDs, baseline input ID and augmented input ID are retained.

Features, all based on 5-minute bars:
- latest close, SMA20 and SMA50 (USD)
- RSI14 with Wilder smoothing; flat series=50, no losses=100
- MACD12/26 and signal9 (USD); EMA seeded by the first period's SMA
- ATR14 Wilder smoothing, using previous close (USD)
- close-to-close returns over 12 and 48 bars (1h/4h, fractions)
- latest volume / preceding 20-bar average (null if average zero)

Same numerical inputs go to all four engines. Probabilities remain uncalibrated;
Kairos confidence is never treated as win probability. Kairos's conservative
local input envelope limit is 8192 bytes for 8b (8a keeps 4096). News is not
shortened to accommodate indicators. Existing per-call/batch budget checks apply.

## Operation

Dedicated installation `/opt/btc-phase8b`, Python from `/opt/btc-predict/.venv`.
Copy only `phase8b/*.py`, `news/*.py`, and `candle_store.py`; no 8a file changes.
`phase8b.config.json` is the verified 8a configuration with schema `phase8b.v1`.
`phase8b.schedule.json` follows the example, has the next future aligned start,
and points at the existing 8a archive/results read-only. Credentials are read
from the existing server-only `/opt/btc-phase8a/.env.phase8a`, never copied into
Git or output files. Production config and data are private to ubuntu.

Install the six `deploy/systemd/btc-phase8b*` units. Prediction and evaluation
can write only `/opt/btc-phase8b/outputs`; collection can write only `data`.
Start the collector once, check all five symbols, then enable the three timers.
Prediction: hourly :00:30; collection: hourly :56:15; evaluation: every minute :50.
No historical forecast catch-up, implicit retries, model fallback or real trades.
The first scheduled forecast may skip if collection/8a baseline is unavailable.

Each 8b forecast uses at most 20 extra calls, independent limits 480/day and
14400/rolling30d; known-cost reservations 25 USD/day and 750 USD/rolling30d.
Together with 8a this is up to 40 calls/hour. These are safeguards, not expected
charges. Kairos fees remain unknown under the existing user-authorized exception.
Evaluation keeps running even if prediction is disabled or its budget is depleted.

Check `systemctl status btc-phase8b{,-collect,-evaluate}.timer`, service exit codes,
`outputs/hourly-v1/prediction-status.json` and `report.json`. A reserved/partial
batch must not be retried: uncertain paid calls retain their reservations.
Rollback prediction only: disable/stop `btc-phase8b.timer`, retaining evaluation
and all data. Full 8b stop: disable its three timers. Do not stop any 8a services.

## Web and validation

The comparison page is read-only, numeric/status fields only, 60-second refresh,
JST display, symbol/time selectors. No provider calls on page views. It shows
8a/8b probabilities, outcomes, technical inputs and separate evaluation tables.
Cross-condition metrics use only identical news IDs, identical realized prices,
and successful answers from all eight calls at the cutoff. Missing/failed calls
are excluded; counts show coverage. Nonoverlap uses UTC epoch multiples of 4h;
hourly overlapping scores are descriptive. Each condition's own scores and
paper PnL remain separately labeled; paper PnL excludes unknown API fees.

Local: `python3 -m unittest discover -s tests_phase8a -q`
and `python3 -m unittest tests_phase8b.test_phase8b -q`.
With the web dependencies: `python -m unittest tests_phase8b.test_phase8b
 test_phase8b_views test_phase8a_views test_ui test_news_views -q` (one line).
The production Web checkout historically excludes the research packages; tests
there require an isolated 8a test-support directory and `/opt/btc-phase8a` on
PYTHONPATH. Do not install test-support code into the running 8a directory.

Tests cover leakage, initial availability, revisions, missing bars, indicator
reference values, frozen shared news, all four adapters' prompts, no replay,
budgets, four-hour maturity, evaluation while disabled, mismatched comparison
cohorts, invalid filters and secret-free public responses.
