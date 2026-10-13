# Generic completed BTC/USD 5m market-data archive

## Why this is independent

The supplied production snapshot confirms Phase 5 has exactly 96 completed and
RV-evaluated rows, ending at target 1791514800. At 1791518400 onward, Phase 6 still
saves 1h target candles and actual return/direction but has no saved Phase 5 RV.
The calibration JSON ends at 1790776500 and contains none of those 5m windows;
market.db contains trades/book samples, not candle rows. Rejection is not the
cause. An independent archive is required after Phase 5 stops.

This collector supports later RV/30m/15m/shadow/HAR research without attaching
market-data availability to prediction experiments. It imports no experiment
runner, uses no .next-stages.lock, makes no model calls, and changes no predictor,
gate, stop rule, sample count, frozen thresholds, hash-protected source, or start
marker. Failure exits only its own oneshot service. Phase 5/6 failures have no
connection to the collector. The web UI never starts a collection cycle.

## Storage and provider

Default: `/opt/btc-predict/candles.db` when code is installed there. The collector
accepts `--db PATH` for a dedicated archive (no experiment/market DB). Phase 6
reads `candles.db` beside its configured btc.db, so keep them in the same folder.
Neither BTC experiment DB nor market.db is migrated. The collector creates the
following new dedicated tables at first invocation:

- `candles_5m`: start_ts INTEGER PRIMARY KEY; open/high/low/close REAL NOT NULL;
  volume REAL nullable; source TEXT NOT NULL; fetched_at INTEGER NOT NULL;
  raw_json TEXT nullable. SQL checks cover 300s alignment, positive prices,
  OHLC bounds, nonnegative optional volume and fetch after completion + 60s.
- `collector_state`: singleton INTEGER PRIMARY KEY; started_at INTEGER;
  first_start_ts INTEGER; source TEXT. One immutable startup floor, separate
  from every experiment. Keep this table/DB across restarts and upgrades.

All times are UTC Unix seconds; start_ts is the start of the 5m bucket.
Source is `coinbase-exchange:BTC-USD:300`. WAL permits bounded concurrent reads;
writer transactions are short and never contain network calls. SQLite permits
one writer at a time. No automatic deletion or checkpoint/vacuum job is added.
A duplicate never changes prices, provenance, fetched_at or raw_json. Conflicts
are counted in journal output and the first saved valid observation is retained.
Raw JSON preserves the validated Coinbase numeric array (optional volume may
be normalized to null); no price rounding or candle generation is performed.

Provider: Coinbase Exchange public BTC-USD candles, via the unchanged
`prepare_phase4.fetch_candles(start,end)` utility, granularity 300. Both explicit
start/end are always passed; each cycle uses one bounded request, not pagination.
[Coinbase documentation](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)
identifies bucket-start timestamps, OHLCV array order, supported 300s granularity,
and missing buckets when there are no ticks. Responses can precede the requested
start, so the collector enforces `[start,end)` locally as well. The documentation
does not guarantee a publication deadline: +60s is a safety delay, not a latency
SLA. Missing/late buckets are retried only within the short recovery window.

## Future-only and recovery

On first invocation, first_start_ts is the next 5m boundary at or after actual
startup time, persisted BEFORE any fetch. A candle that started before this floor
is never fetched or saved. First invocation usually just initializes and waits.
Retries/restarts retain the floor even if the first network request fails.

At each invocation:

1. Compute end = floor((now - 60) / 300) * 300.
2. Compute start = max(first_start_ts, end - 6*300).
3. If start >= end, wait until a later timer invocation without fetching.
4. Otherwise fetch `[start,end)` once; validate and insert only missing rows.

The latest considered bucket ends at end, at least 60s before collection time.
The service runs at :01, :06, :11, ... UTC. No current/incomplete bucket is saved.
A short outage can recover the last six completed buckets (30 minutes); it cannot
recover hours/days. No arbitrary date/start override or historical backfill CLI
exists. Provider omissions stay gaps. No interpolation, zero fill, rounded
prediction-text reconstruction, trade reconstruction or synthetic candles.
Do not delete collector_state to reset collection; doing so changes the startup
floor and does not repair gaps.

Normal timer startup at minute :01/:06/... produces its first newly completed
5m row roughly 10 minutes later, assuming provider availability. Manual startup
at other times produces first rows about 6–11 minutes later. First complete
hourly RV needs the preceding candle plus all 12 hour candles; depending on start
position within the hour this typically takes about 65–125 minutes. Provider or
scheduler gaps can delay it further. Collector-start hours may be unavailable.

## Phase 6 visibility

Read-only priority, for completed hours regardless of gate:

1. Confirmed saved Phase 4/5 evaluated RV.
2. Thirteen complete validated closes in candles.db, from target-300 through
   target+3300, using unchanged assign_rv -> volatility.realized_volatility.
3. Existing exact local JSON archives as compatibility fallback.
4. Unavailable when no complete local window exists.

Classification uses the existing Phase 4 frozen thresholds from the saved Phase
6 config. RV stays nonannualized percent, exactly twelve consecutive log returns.
No additional RV definition is implemented. Dedicated archive windows are
validated independently; RV availability does not depend on a 1h target fetch.
Malformed/missing/locked/corrupt archive reads return unavailable, not a route
error. Open hours stay pending. Display formats remain +/-0.1234%, 0.4567%, and
QUIET/NORMAL/ACTIVE. Rows before collection can remain unavailable forever and
are explicitly described as such in the UI. The archive does not backfill or
re-score predictions or change experiment metrics/counts.

## Growth estimate

288 rows/day, 105,120 rows/year. A local measurement using the real schema and
representative BTC OHLCV/raw JSON inserted 8,640 rows (30 days); checkpointed DB
size was 1,286,144 bytes (~1.23 MiB). Linear estimate ~15 MiB/year, allow roughly
15–30 MiB/year for varying raw numeric lengths/page utilization, plus transient
WAL/SHM files. Measure real deployment over time; no retention deletion needed.

## Validation

71 unittest tests pass, including the existing Phase 5/6 suite and route tests.
New coverage checks future-only startup floor, completion/safety delay, duplicate
idempotency, malformed OHLC/timestamps/volume, recent-gap recovery, bounded
requests after long downtime, canonical 13-close RV and frozen regimes, all four
gate cases via dashboard/JSON routes, missing/invalid/corrupt stores, saved
confirmed evaluation priority, no fetch from reporting, and unchanged experiment
DB bytes, model-call counts and frozen implementation hashes on collector failure.
Provider calls are mocked; no real market-data/model API was invoked in testing.
Existing static-file ResourceWarnings remain non-failing. Systemd runtime is
operator-verified on Linux as described below.

## Operator deployment (no server changes performed by this task)

Confirm the existing checkout/service paths and unprivileged account. Substitute
verified paths/account if different; preserve tracked local changes before pull.

```sh
systemctl show btc-web.service -p WorkingDirectory -p ExecStart -p User -p Group
cd /opt/btc-predict
git status --short --branch
git pull --ff-only origin phase6
.venv/bin/python -m unittest discover -q

sudo install -m 0644 deploy/systemd/btc-candles.service /etc/systemd/system/btc-candles.service
sudo install -m 0644 deploy/systemd/btc-candles.timer /etc/systemd/system/btc-candles.timer
sudoedit /etc/systemd/system/btc-candles.service
# Set User/Group to the existing unprivileged checkout owner.
# Also adjust WorkingDirectory/ExecStart/ReadWritePaths if paths differ.
# Ensure that account can write the directory and btc-web can read candles.db.

sudo systemd-analyze verify /etc/systemd/system/btc-candles.service /etc/systemd/system/btc-candles.timer
systemd-analyze calendar '*-*-* *:01/5:00 UTC'
sudo systemctl daemon-reload
# Establish the future-only startup marker now (first run normally fetches nothing).
sudo systemctl start btc-candles.service
sudo systemctl enable --now btc-candles.timer
sudo systemctl restart btc-web.service

systemctl is-active btc-candles.timer btc-web.service
systemctl list-timers btc-candles.timer
journalctl -u btc-candles.service -n 30 --no-pager
curl --fail https://btc.kenic.jp/direction-active/
curl --fail 'https://btc.kenic.jp/analyze/?phase=phase6'
curl --fail https://btc.kenic.jp/direction-active/data.json
```

The service is oneshot, so `inactive (dead)` after successful completion is normal;
inspect its Result/ExecMainStatus and journal rather than expecting it to stay
active. Timer Persistent=false avoids missed-calendar catch-up runs; every cycle
still computes its window from the current clock. Service failures are retried
on the next timer firing, with no dependency on experiment services/locks.

Web restart is required to load the new archive-reader code. No Phase 5/6 start,
evaluation, gap-fill, migration or manual scheduler invocation is required. No
changes/restarts to existing experiment timers, book/trade collectors or model
services are needed. Existing experiment/calibration files remain untouched.
SQLite schema initialization is automatic and confined to the new candles.db.
The systemd files are templates; this macOS implementation environment cannot
execute Linux systemd. Verify the units on the server before enabling them.

Rollback: disable/stop btc-candles.timer and let any collection cycle finish;
restore the previous reporting code and restart btc-web. Keep candles.db and
all experiment DBs/start state; do not restore an older experiment DB.
