# Phase 6 — prospective high-confidence-ACTIVE direction experiment

Development branch: `phase6`, based on `phase4r-phase5` at
`624eae740cd3a36b81d4d099eb0a3f54be43e12f`, tagged `phase6-base`.
The verified implementation release is tagged `phase6-start`. This release tag
identifies deployable code; the actual experiment start is a separate explicit
server action, persisted with timestamp, first future target, config SHA-256,
Git commit and implementation hashes. Deploying code does not start predictions.

## Frozen design

The effective sample count and stopping rule are **96 accepted distinct target
hours**, irrespective of missing methods or their results. There is no optional
stopping. Ten volatility calls on one snapshot are repetitions, not ten samples.
Phase 6 can run longer than the existing 96-prediction Phase 5; Phase 5 retains
its own original stopping rule and continues unchanged.

Volatility repeated GPT classification is called **Repeat agreement** or
**10-shot repeat agreement**. The gate requires all three conditions:

- Single-shot volatility = ACTIVE
- Repeat modal class = ACTIVE
- Repeat agreement >= 8/10

All ten repeats must be valid. **ACTIVE repeat agreement >= 8/10** means at
least eight of the ten repeat classifications are ACTIVE. The Phase 4
thresholds, prompt, probability validation and QUIET/NORMAL/ACTIVE tie order are
reused. Original single-shot refers to slot zero, distinct from the ten repeats;
when an equivalent existing Phase 4 record is present it is reused instead of
issuing another single-shot. New live slots are stored exclusively in Phase 6
and never extend Phase 4's experiment. A non-ACTIVE or failed single-shot rejects
the gate without repeats or direction calls. Incomplete/invalid repeats reject
the gate. No research shadow mode is enabled.

Every method receives the exact same stored target and snapshot. The Phase 4
snapshot builder is reused without changes. Saved text, its hash, deterministic
numeric fields parsed from that text, raw provider responses, returned models,
rule versions, call timestamps and gate details are retained. Parsing the rounded
text prevents fetching different or late-arriving microstructure for a rule.
Target alignment requires the reference candle to start at target minus 3600.
Reference price is the reference close displayed in that exact snapshot.
The target remains next completed 1h close versus reference completed 1h close;
UP/DOWN/FLAT semantics match Phases 1–3. Realized target candle is saved verbatim.

Methods (all `phase6-v1`):

- OpenAI probability prediction with Phase 3 information and direction semantics.
- Sign of previous 1h close-to-close return; opposite sign reversal; always-UP.
- Short momentum: sign of mean 5m and 15m returns from the 5m frame.
- Breakout: final completed 5m close above prior eleven 5m highs or below their
  lows; no breakout abstains.
- Trend: equal sign vote of SMA20 minus SMA50 and MACD histogram; tie abstains.
- Order flow: sign of preceding 60m aggressive BTC-volume buy ratio minus 0.5.
- Order book: equal sign vote of preceding 60m top10 imbalance minus 0.5 and
  microprice offset; tie abstains.
- Expanding L2 logistic regression described below.
- Majority-vote ensemble (Direction ensemble) of eligible base methods (at least three; tie abstains), and
  Probability-average ensemble (Direction ensemble; at least two actual probability methods; 0.5 predicts UP).
  Direction ensembles never include other Direction ensembles or assign invented probabilities to
  rules. Membership is saved. All base methods receive equal weight.

Zero, unavailable or non-finite rule signals abstain. Method failures remain
unavailable with reasons; other methods continue. A prospective gate and method
call must begin within ten minutes of its target boundary. A gate finishing
later is rejected as late; individual outputs finishing later retain their
original call and response timestamps. No predictions are sent after that
window and no historical predictions are backfilled.

## Statistical classifier and evaluation

Logistic training uses available, saved Phase 2/3 and Phase 6 snapshots from all
hours, not just retrospectively ACTIVE hours. It deduplicates each target hour.
Historical rows must have both `target + 3600 < current_target` and
`evaluated_at < current_target`. FLAT labels and malformed/missing feature rows
are excluded. Phase 1 lacks the required representation and is unavailable for
training. Historical snapshots are not reconstructed. Newly fetched evaluation
labels cannot enter a current fit. Training is performed independently at each
accepted gate, with at least 100 complete labeled hours and ten per class.
Until then the method, and potentially the Probability-average ensemble (Direction ensemble), are unavailable.

The features are 1h return, 5m return, 15m return, SMA percentage gap, normalized
MACD histogram, 60m buy ratio, top10 imbalance and microprice offset. Scaling
uses training data alone; constant columns have scale one. NumPy full-batch
logistic regression uses 400 deterministic steps, learning rate 0.1 and L2=0.01
(excluding intercept). No hyperparameter search or interim tuning occurs.
Training row IDs, count, cutoff, data hash, feature names, means/scales and fitted
coefficients are saved. This is an interpretable experimental baseline rather
than an optimized trading model. No new dependency is required.

Reports show per-method N/correct/accuracy, probability-only Brier and its N,
unit-position signed 1h percent PnL and its N, mean/median absolute target return,
and available prediction coverage over scheduled hours, observed single-shot
ACTIVE hours and accepted gates. Missing scheduler hours count in scheduled-hour
coverage and their ACTIVE classification remains unknown. FLAT is excluded
from accuracy/Brier and contributes zero PnL. Fees, spread and position sizing
are excluded. Reported metrics can use different N; those counts are explicit.

`/analyze/?phase=phase6` compares all methods/Direction ensembles on common scored events,
including accuracy, PnL and probability-intersection Brier differences. Two-sided
exact McNemar p-values appear from 20 common events. These multiple comparisons
are exploratory and unadjusted; they are not confirmation of an edge.

The research premise is preserved: retrospective ACTIVE direction accuracy in
Phases 1–3 was approximately OpenAI 34.9% / Jev 41.9%. Phase 6 does not assume
ACTIVE is easier; it asks whether alternative signals have conditional edge
when ACTIVE is prospectively identified with high confidence.

## Frequency, cost and runtime boundaries

There is no current live DB in the development checkout; actual prospective
gate frequency cannot be verified here. Calibration's one-third ACTIVE share
is not the prospective gate rate. The read-only `--frequency` command inspects
existing frozen OpenAI Phase 4R sources using the actual fixed gate; its results
are saved into the preregistration at start. Incomplete 10-shot repeat agreement results are reported and
excluded from accepted historical gates. Historical repeat results are an
estimate, not prospective evidence, and the 96-gate stopping rule stays fixed.

For scale only: 96 gates take about 40 days at a 10% calendar-hour gate rate,
20 days at 20%, or 12 days at one third. Actual frequency may be much lower or
zero. There is no added maximum-duration stop. Every observed hour costs one
new volatility call unless cached; single-shot ACTIVE adds ten repeats, and an
accepted gate adds one direction call. Repeats use ten parallel workers with a
120-second provider timeout and SDK retries disabled; provider failures consume
their slots rather than triggering replacement calls. Account limits can reduce
coverage. Rules/logistic and GPT execute in parallel on the same input.

The existing `.next-stages.lock` serializes hourly/repeat/operator processes.
Phase 6 runs alongside Phase 5 inside that lock with independent failure handling.
An optional `--repeats-only` worker remains restricted to Phase 4R. No Phase 6
calls occur during Phase 4/4R or before the explicit start. Phase 6 exclusively
writes four additive tables: `phase6_config`, `phase6_events`, `phase6_calls`,
`phase6_predictions`; original tables and data are untouched.

Hour reservations and `(target,method)` / `(target,slot)` primary keys prevent
replays. Equivalent caches require same target, byte-identical snapshot, original
model and frozen volatility configuration; ten completed valid 4R slots are
required for repeated-result reuse. An old timestamp is never substituted for
a new live target. Pending interrupted gates become rejected; interrupted method
reservations become unavailable. Provider calls cannot be guaranteed exactly
once across a remote/local crash, but persisted attempts are never reissued.

## Terminology-only UI updates

“Ensemble” refers exclusively to combinations of direction predictors:
**Direction ensemble**, **Majority-vote ensemble**, or **Probability-average
ensemble**. Volatility uses **Repeat agreement**, **10-shot repeat agreement**
and **Repeat modal class**. Internal method IDs and frozen metadata may retain
legacy identifiers; presentation aliases clarify their meaning without changing
stored records, configuration hashes or implementation hashes.

For this terminology-only update on an already deployed `phase6` checkout:

```sh
cd /opt/btc-predict
git pull --ff-only origin phase6
.venv/bin/python -m unittest test_phase6 test_ui -q
sudo systemctl restart btc-web.service
systemctl is-active btc-web.service
```

Only the web service needs restart. No migration, timer/worker restart or new
`--start` invocation is needed. Keep the existing start state and both release
tags unchanged. The original deployment steps below apply to initial deployment.

## User-operated server deployment

No live server operation was performed by this implementation task. Use the
existing production checkout documented as `/opt/btc-predict`, and verify the
`btc-web.service` working directory before proceeding. If it differs, substitute
that verified directory. Preserve/reconcile local tracked modifications first.
Do not upload local DBs, calibration JSON, `.env`, backups or enable markers.

Run away from the hourly boundary. Holding the existing lock during checkout
prevents mixing scheduler versions; a scheduled invocation encountering the lock
skips that cycle. Collectors remain running. The command below waits for any
current hourly/repeat process to finish and makes a consistent SQLite backup.
The server must have `flock` (standard Linux util-linux).

```sh
systemctl show btc-web.service -p WorkingDirectory -p ExecStart
cd /opt/btc-predict
git status --short --branch
git rev-parse HEAD  # retain the rollback commit
git fetch origin phase6 --tags
flock .next-stages.lock sh -eu -c '
  .venv/bin/python -c '\''import sqlite3; c=sqlite3.connect("btc.db"); c.backup(sqlite3.connect("btc.db.before-phase6"))'\''
  git switch phase6
  git merge --ff-only origin/phase6
  git merge-base --is-ancestor phase6-start HEAD
  .venv/bin/python -m unittest discover -q
  .venv/bin/python -c '\''from phase6 import migrate; migrate()'\''
'
sudo systemctl restart btc-web.service
systemctl is-active btc-web.service
curl --fail https://btc.kenic.jp/direction-active/
curl --fail 'https://btc.kenic.jp/analyze/?phase=phase6'
curl --fail https://btc.kenic.jp/direction-active/data.json
```

Only the web service needs restart. Existing hourly jobs must already run
`run_hourly.sh` / `next_stages.py`; they load the new code on their next invocation.
Timers, collectors and optional repeat workers do not need restart or new jobs.
Do not manually invoke the live scheduler to test it. Existing Phase 5 continues
with its original configuration, state, call/evaluation path and 96-success stop.
No original experiment marker is changed by migration or Phase 6 start.

Inspect historical gate frequency and start explicitly after Phase 5 is enabled:

```sh
cd /opt/btc-predict
.venv/bin/python phase6.py --frequency
.venv/bin/python phase6.py --start
.venv/bin/python -c 'import json,sqlite3; c=sqlite3.connect("btc.db"); print(json.dumps(json.loads(c.execute("SELECT config_json FROM phase6_config").fetchone()[0]),indent=2,sort_keys=True))' > /tmp/phase6-frozen-config.json
```

`--start` waits for the same lock, checks frozen Phase 4 calibration against its
existing DB copy, requires a committed clean tracked checkout and Phase 5's
existing enabled marker, then records the **next** hourly boundary. It does not
issue model calls or restart anything. Repeating it does not reset the timestamp,
config or stop count. An immutable record remains in the DB; the JSON export is
only a review copy. Source-hash drift after start blocks Phase 6, preserving the
preregistration, while Phase 5 can continue. Do not edit method code after start.

Rollback: acquire `.next-stages.lock`, restore the saved prior code commit, and
restart `btc-web.service`. Preserve all additive tables and their start marker.
Do not restore an older DB over current live observations or rerun Phase 4.
Returning to the tagged Phase 6 code resumes only future hours; missing past
predictions remain missing.
