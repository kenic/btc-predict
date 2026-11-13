# Phase 8d — BTC / XRP news-event experiment

One prospective experiment: first-observed relevant English news plus technical
indicators, four concurrent model calls, outcome at receipt + 4h. No live trading.
8pre retains news; 8pre2 owns technical/quote collection; 8d owns events and scores.
Old 8a/b/c records remain separate and their evaluation timers continue.

## Frozen comparison

BTC and XRP only. A committed RSS batch creates at most one event per relevant
asset at its actual `available_at` (fractional UTC epoch). The prediction worker
checks the original news DB every second, not the hourly export. News acquisition
is polled once per minute; publisher-to-RSS lag remains observable, not eliminated.
First startup seeds a cursor and duplicate identities; historical articles never
fire a paid backlog. Only the first version of an article triggers. Later revisions
are archived but do not retrigger. Canonical URL or exact normalized title+summary
duplicates do not retrigger. Semantically similar reports with different wording
may still trigger; this initial rule intentionally makes no semantic clustering claim.
A crypto/macro-wide relevant item may trigger both assets. Publication must be in
(T-24h,T], permitted for model input and source healthy. Initial source set is the
existing operator-approved feeds, with no new licensing assumption.

Trigger articles lead the input, followed by bounded 24h background (5 articles,
2400 UTF-8 bytes). Full trigger IDs and article-version IDs are retained even when
input caps exclude some. At least one trigger must be in model input. The exact
news list, raw historical snapshot, technical bar versions and complete input
hash are immutable and common to all engines. All input receipt times <= T.
Technical state comes only from 8pre2: no book/trade-flow input in this experiment.
A missing/stale required input produces `unavailable`, never fabricated neutral data.

GPT-6 Astra / Claude Fable / Jev reuse verified adapter interfaces, low effort,
fixed production model IDs. Kairos uses its dedicated key, POST `/v1/systemone`,
`general` only; provider confidence is not the UP probability. No probability
rescaling; all are uncalibrated. Native raw responses remain private. Public Web
returns only allowlisted fields. No model calls in the Web process.

Claim and all four budget reservations commit before dispatch. No automatic paid
retry, including unknown-charge timeouts. Restart marks in-flight calls unknown;
it does not resend. Starts must be within 60s of T; completion deadline 120s.
An overfull queue produces visible expired events rather than historical live calls.
Daily known-cost reservation cap $100, rolling 30d $3000, 800 calls/day. Kairos cost
is unknown and excluded from these known-cost limits, under prior explicit user
permission. $0.50 reserved per call, conservative estimates rather than settled bills.

## Two evaluations

Prediction reference: latest midpoint received <= T, no more than 10s old.
Target: first quote requested >= T+14400 and received within 15s. Direction uses
midpoints, exact flat is DOWN. Actual observation times and quote IDs are retained.
This is a bounded polling approximation, not exact exchange-time prices.

UP >= .55, DOWN <= .45, ABSTAIN strictly between. Abstentions are excluded only
from directional accuracy; all valid evaluated probabilities enter Brier/LogLoss.
API/input/price failures are separately counted. Shared four-engine cohorts and
all available engine observations are both in JSON; Web foregrounds shared cohorts.
Nonoverlap events are greedily selected per asset by T then event ID, reserving
[T,T+4h) even for failed events, before looking at outcomes. No return-based selection.

Paper entry: each engine's first quote whose request starts AFTER its completed
response, received within 15s; reject entry at/after target. Exit is the same fixed
T+4h quote, not completion+4h. Buy at ask, sell at bid. $1000 fixed notional,
5 bps fee and 5 bps slippage each side, plus observed spread. Shorts are synthetic;
borrow costs, depth/size fills and funding are not modeled. Report event sums and
per-engine/per-asset nonoverlapping holdings separately, without compounding.
Missing exit data is explicitly unavailable, not filled with a later convenient price.
Unknown-position intervals conservatively block overlap through target + 15s.

## Operations / migration

Runtime `/opt/btc-phase8d`; immutable config `phase8d.policy.json`, data
`data/events.sqlite3`. `phase8d.cli init` initializes the future-only cursor.
`check` reports credential-presence booleans only. `smoke` explicitly calls all four
engines for both assets using current archived news; these `validation` events are
excluded from every score and do not claim to be fresh-news events.

1. Test isolated source tree; deploy and warm up 8pre2; verify both assets' bars/quotes.
2. Install 8d isolated, initialize, run validation, require all eight responses OK.
3. Start prediction + evaluation services and verify health/Web.
4. Disable ONLY `btc-phase8a.timer`, `btc-phase8b.timer`, `btc-phase8c.timer` and set
   their schedule `enabled` flags false. Let any already-active prediction complete.
   Do not stop their evaluation timers, collectors, exports, or delete any output.
5. Keep old pages as historical experiments; do not combine their metrics with 8d.

Pause new 8d dispatch: create `events.sqlite3.paused` beside the DB, or stop
`btc-phase8d.service`. Evaluation and 8pre2 continue. While paused no new calls run;
stale events on resume are recorded unavailable, not backfilled. Never edit a
running experiment's contract or reset the cursor to retry calls.

Rollback: stop 8d prediction (leave its evaluation and archives), restore the three
backed-up schedule JSON files and re-enable their prediction timers. Restore news
polling configuration/timer if necessary. 8pre2 is independent and can remain on.
No credentials in code, command arguments, Web payloads or logs; use the existing
server-only dotenv file via its pathname. All timestamps stored UTC, Web displays JST.

Tests: `python -m unittest tests_phase8d.test_phase8d -v` plus existing Phase 8/UI
regression tests. Fixtures cover no replay, dedup/revisions, input availability,
quote timing, thresholds, costs, API failures, budget reservations and HTML escaping.

### Legacy collector compatibility

The standalone `/opt/btc-news` collector originally enforced a 600-second minimum.
Apply `deploy/enable_news_minute_interval.py --root /opt/btc-news` before configuring
60-second polling. The tool accepts only the known legacy source or its own patched
version, saves an original backup and records before/after hashes. This change is
collector-only: do not replace the frozen `news/` copy inside an active 8d runtime.
Source model-input policies and Retry-After remain unchanged. Verify a successful
scheduled run with `interval_seconds=60`, not merely an active timer.
