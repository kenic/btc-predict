# Phase 8c — Phase 6 feature family plus archived news

8a and 8b remain unchanged. 8c adds a separate prospective, hourly, four-hour
experiment for BTC, ETH, SOL, XRP and LINK and the same four engines. No trading.
The isolated runtime is `/opt/btc-phase8c`; `/phase8c/` shows 8a/8b/8c side by side.

## Comparison contract

Reuse the exact 8a manifest at the same cutoff, including all news/article-version
IDs. Never reselect news. Match its provider models, effort (low), prompts,
Kairos `general` profile, output limit, outcome definition and four-hour horizon.
Only technical state changes from 8b. No Phase 6 volatility gate or 1h target.
Predictions use the existing uncalibrated schema; confidence is not win probability.

Candles: 200 first-observed, immutable bars at each of 1h/15m/5m. Reuse Phase 6's
`calculate_timeframe_indicators` implementation (vendored and source-hashed):
pandas EWM RSI/MACD, SMA20/50, sample-std Bollinger position, current-inclusive
20-bar volume ratio. Include returns through 24h, and 12 raw 5m OHLCV candles.
All three frames include SMA/Bollinger, a small superset of Phase 6's text input.
Unit labels are explicit: returns in percent, prices USD, volume in base asset.
Undefined ratios remain null, not fabricated neutral values.

Availability is stricter than the legacy Phase 6 context: a frame's latest bar
ends **one complete interval before cutoff** (1h/15m/5m respectively), with 60s
settlement and first observation <= cutoff. Every expected bar is required.
Do not silently relabel this as an exact reproduction of Phase 6.

Microstructure: current book, top5/10/20 imbalance, spread, microprice offset,
and orderbook averages / aggressive trade flow for 1/5/15/60 minutes. BTC uses
existing `/opt/btc-predict/market.db` read-only, filtering created_at/collected_at.
The legacy trade collector can miss trades during bursts; 8c supplements it
with bounded pagination and validates contiguous BTC trade IDs across the window.
BTC book collection is not duplicated; existing collectors are not modified.
Other assets use a dedicated 15s public collector. Trade side is Coinbase's maker
side and is inverted for aggressor volume. The first poll is warm-up; subsequent
polls must overlap the previous ID, using up to ten pages of 1000. Missing overlap,
>45s collection gaps or >30s staleness reject input. All returned rows carry actual
receipt times; no availability backdating, late bar substitution or catch-up calls.

The cohort stores a hashed technical snapshot with candle IDs/raw versions,
book observations, trade IDs or a verified contiguous ID range, and content hashes.
Trade raw records remain in the source SQLite archives; do not delete those DBs
while retaining the experiment. Source versions, frozen state, requests, raw
provider responses, errors, times, cost estimates and latency remain auditable.

## Operation

Copy `phase8c/`, `news/`, `candle_store.py` into the dedicated runtime. Config is the
verified 8b config changing only schema to `phase8c.v1`. Do not copy credentials:
read the existing server-only `/opt/btc-phase8a/.env.phase8a`. The Web needs only
`phase8c_views.py`, its shared views, route and nav; it never calls paid providers.

Install `deploy/systemd/btc-phase8c*`. Start micro collection and candle collection
first. Allow a full hour of micro history, and set start_cutoff to the next future
UTC hour after warm-up. Every required asset must pass preflight before any paid
call: if one is incomplete, the whole 20-call cohort is skipped once. Existing
8a/8b continue. No retries of a reserved, partial or failed cutoff.

- Micro: continuous public GETs, roughly every 15s, bounded pagination.
- Candles: hourly :57:00 UTC (initial fetch required).
- Prediction: hourly :00:45 UTC, input cutoff on the hour, start <=60s, deadline120s.
- Evaluation: every minute :55, beginning target_end+60s. Continues when forecasts
  are disabled. Uses the same price archives/definition as 8a/8b.

8c retains a 0.25 USD per-call reservation; batch known-price bound <=2 USD,
480 calls/day and 14400/rolling30d; schedule reservation ceilings 100 USD/day and
3000 USD/30d. These are conservative safeguards, not expected bills. The inherited
Kairos fee exception is explicit and total fees remain unknown. Local Kairos
request-envelope guard is 16384 bytes for the larger technical input; this is a
local guard, not a claimed provider context limit. No news is truncated to fit.
All three phases together can use up to 60 calls/hour.

Check `systemctl status btc-phase8c-micro.service`, the three timers, and
`outputs/hourly-v1/{prediction-status,report}.json`. Check disk usage: archives
are append-only, with no automatic retention deletion. Stop just 8c predictions
with `sudo systemctl disable --now btc-phase8c.timer`; retain evaluation and data.
Full rollback stops the micro service and three 8c timers, reverts the Web route/nav,
and never changes or deletes 8a/8b files or archives.

## Evaluation and tests

Web pair comparisons (8a vs 8c, 8b vs 8c) require all eight successful answers,
the same cutoff, 4h target, news ID and realized prices. Pair denominators can differ.
Keep legacy 50% accuracy plus selective accuracy (p<=.4 DOWN; p>=.6 UP; otherwise
abstain), abstention rate, Brier and LogLoss including abstentions. Four-hour
nonoverlap is primary; overlapping hourly scores are descriptive. Existing paper
trading costs remain, unknown API fees are not silently treated as zero.

Run with project dependencies:
`python -m unittest tests_phase8c.test_phase8c test_phase8c_views tests_phase8b.test_phase8b test_phase8b_views test_phase8a_views test_ui test_news_views -q`
Tests use fixture model calls, not paid providers. Test source availability,
revisions, missing bars, micro gaps, maker-side inversion, frozen common input,
no replay, budget gates, maturity, disabled-prediction evaluation and private-data
exclusion. Live first prediction / first four-hour result must be checked separately.

Coinbase trade-side and pagination specification:
https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-trades

## BTC trade gap repair (2026-11-13)

The 12:00, 13:00 and 15:00 JST skips had 17, 140 and 120 missing BTC IDs,
respectively. Neither archive contained these IDs. Representative missing IDs
1120598348, 1120796381 and 1121007949 were returned by the public trade API during
a later diagnostic lookup. Diagnostic replies were kept outside the input archive;
they do not establish that those trades were available at the original cutoffs.
The original poll responses were not retained, so the exact upstream reason for
the initial omission cannot be reconstructed. The confirmed collector defect was
that overlap with the previous maximum ID did not trigger repair of internal gaps.

`phase8c.micro.repair_recent` now rechecks the most recent 65 minutes of saved BTC
IDs after normal collection. It uses bounded public cursor requests at the known
right-hand side of a gap, at most two pages of 1000 per poll. Unresolved gaps are
retried on later collection cycles. The same GET endpoint/cursor behavior was
verified against the actual missing IDs. No prediction-time fetching is added.
Reference: https://docs.cdp.coinbase.com/exchange/rest-api/pagination

Every newly found row is first observed at actual repair response time. Existing
rows and timestamps are never overwritten. Future-dated/malformed responses are
rejected; attempts, response hashes and insertion counts are stored in the new
`trade_repairs` audit table. Gaps that cannot be repaired before cutoff still fail
the original strict completeness check. The 30s freshness/45s book coverage rules,
news manifest, all model/prompt settings and the 4h target are unchanged.

This is an 8c-only collector release. Preserve original files and contract, copy
existing run artifacts/SQLite snapshots and ledger reservations into
`/opt/btc-phase8c/outputs/trade-repair-v1-20261113`, and start a new contract at a
future hourly cutoff. Carry `news-policy.json` with the original shared-news
transition time, so the Web does not label this as a news-policy change. Switch
only the 8c schedule/output and Web root. 8a/8b services, code and settings remain
unchanged. Original skipped cutoffs are not retried or relabeled as successes.
No ChatGPT monitoring automation is resumed.

Tests: `python -m unittest tests_phase8c.test_trade_repair
 tests_phase8c.test_phase8c -q` (one line), plus shared-news and Web regression tests.
When validating repairs on live data, inspect audit counts and immutable observed
times; never backdate a repair to make a historical prediction pass. Check the first
new scheduled cohort separately. To roll back, stop only 8c prediction/evaluation
and its micro collector briefly, restore the saved 8c release/schedule and Web root,
then restart them. The extra audit table is additive and can remain in the archive.
