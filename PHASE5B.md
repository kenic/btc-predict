# Phase 5b — independent hourly RV observation

`phase5b-v1` observes **500 consecutive hourly target slots**, not 500 successful
responses. No optional stopping, adaptive predictor changes, selector, direction
calls or simulated trades. `init` fixes the next UTC hour as `start_target` and
`end_target = start_target + 499*3600`. Failures, uncertain attempts and missed
hours consume their original slots; they are never replaced with later hours.

## Isolation and frozen predictor

Only new files and `phase5b_config` / `phase5b_predictions` tables are added.
Phase 6's gate, direction methods, 96-event stop, preregistration and hashed
source files are untouched. No Phase 6 configuration initialization is called.
The service has no dependency on, restart action for, or lock shared with the
existing hourly service. DB transactions are short (1s SQLite busy timeout),
and no fetching or API work happens while holding a write transaction.
Resource/API capacity is still shared: check provider capacity and service
account before enabling the additional hourly request.

Phase 5b calls the existing `next_stages.regression_call` and
`phase4_runner.build_snapshot` unchanged. Refactoring those files would invalidate
Phase 6's frozen implementation hashes, so no shared-source extraction is done.
The exact predictor source, requested model, source SHA-256 hashes, dependency
versions, Python runtime and design are persisted in `phase5b_config` before
observations; each cycle verifies them. Prompt, features, provider defaults and
validation remain Phase 5's. There are no retries (`max_retries=0`). The configured
model is currently `gpt-5.6-sol`; returned provider model metadata is recorded on
each successful response. A provider-managed model alias/default can change
externally; local freezing cannot guarantee immutable provider weights.
Git HEAD is recorded as `code_sha`; finalize/commit code before production init.
No API keys are saved in the experiment config.

`target_candle_time` is the UTC Unix **start** of the predicted hour, exactly the
meaning of `phase6_events.target`. The first target close is the close of the
5m candle starting at target, with its preceding close from target-300.
Actual RV uses the unchanged canonical helper:

```
100 * sqrt(sum(log(C_i/C_(i-1))**2 for i in 1..12))
```

13 consecutive closes, percent, nonannualized; no interpolation or zero-fill.
Previous RV uses Phase 5's snapshot builder. Actuals read the existing independent
`candles.db` in read-only mode after target+3660. Missing/invalid windows stay
`actual_rv=NULL, evaluated_at=NULL`, and later cycles may resolve them. The
collector must already be running; Phase 5b does not initialize or backfill it.

## Restart and timing semantics

The independent timer runs at `hh:01:30 UTC`, within the existing 600s prediction
window. It covers every hour regardless of ACTIVE / QUIET / NORMAL or Phase 6's
completion. First missing DB init fails without making provider calls.

A transaction claims a unique target before fetching. Duplicate invocations
cannot claim it again. Statuses:

- `complete`: validated response received within 600s; eligible for analysis.
- `failed`: input/provider/validation failure, no retry or replacement.
- `started`: reserved attempt interrupted or still running; uncertainty is
  retained, no automatic retry, subsequent targets continue.
- `missed`: scheduler ran too late or recovered after downtime; no hindsight call.
- `late`: returned after 600s; value/raw response retained but excluded from analysis.

Snapshot text, previous RV and its SHA-256 are saved before the provider request.
Successful responses retain full provider JSON. The unchanged Phase 5 adapter
cannot supply raw JSON if it raises before returning (e.g. JSON validation error);
those failures retain the exception and a NULL raw field. `started_at` is the
actual attempt/detection time, including for retrospectively recorded missed
slots; it is not a fabricated historical timestamp. `completed_at` records
terminal processing, while evaluation availability is separate.

The timer may keep running after target 500 to recover final actuals. No model
calls or new targets occur outside the registered window. Keep it active at
least until the final hour closes; stopping it does not turn missing actuals into
zeros. Monitoring result quality must not change this window or predictor.

## Operator deployment (not executed by this change)

Before deploying, confirm production Git status/branch, active systemd units,
current frozen hashes and schema. This local checkout is not `/opt/btc-predict`;
production state needs a separate SSH inspection. Do not switch the production
branch or pull unrelated changes into the running Phase 6 checkout. Transfer
only these six new files from the reviewed commit, or use the site's established
additive deployment process, and verify Phase 6 hashes still match its stored
configuration. Never rerun Phase 6 start/init to accommodate code drift.

Run checks first in the review checkout:

```sh
.venv/bin/python -m unittest discover -v
git diff origin/phase6 -- phase6.py phase6_methods.py phase4_runner.py volatility.py next_stages.py predict_gpt.py indicators.py microstructure.py
```

On the server, inspect (read-only) existing service names and Git/DB state:

```sh
cd /opt/btc-predict
git status --short --branch
systemctl list-timers --all
systemctl list-units --all 'btc*'
sqlite3 -readonly btc.db '.schema'
```

Confirm `candles.db` collection is healthy and the unit's `User=btc` / `Group=btc`
match the existing unprivileged production account. Create a consistent SQLite
backup using the backup API (avoid copying only the live DB file under WAL):

```sh
cd /opt/btc-predict
mkdir -p backups
.venv/bin/python -c 'import sqlite3,time; s=sqlite3.connect("file:btc.db?mode=ro",uri=True); d=sqlite3.connect("backups/btc-before-5b-"+str(time.time_ns())+".db"); s.backup(d); d.close(); s.close()'
```

Then register once, as the production service account, after final code deployment.
The first observation starts at the next UTC hour. Repeating init validates the
original freeze and does not reset its start or extend the window.

```sh
sudo -u btc /opt/btc-predict/.venv/bin/python /opt/btc-predict/phase5b.py init
sudo install -m 0644 deploy/systemd/btc-phase5b.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/btc-phase5b.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btc-phase5b.timer
.venv/bin/python phase5b.py status
journalctl -u btc-phase5b.service --no-pager -n 50
```

Do not enable/start the service directly to manufacture extra forecasts. A
manual cycle within the registered target window is idempotent, but provider
requests cost money. To stop the independent scheduler after the experiment:

```sh
sudo systemctl disable --now btc-phase5b.timer
```

Do not remove the experiment tables or rewrite frozen config during rollback.

## Exploratory analysis

```sh
.venv/bin/python phase5b_analysis.py --db btc.db
.venv/bin/python phase5b_analysis.py --db btc.db --csv > /tmp/phase5b-phase6.csv
```

Read-only LEFT JOIN retains every recorded 5b slot, even if Phase 6 has no event,
rejects the gate or has already stopped. Percentile cuts (0–50, 50–75, 75–90,
90–100) use **all complete on-time 5b predictions**, not just ACTIVE/accepted
hours. Equal values at a cut go to the higher bin; bins may be empty.
The report shows each method's available N, accuracy denominator, accuracy,
mean signed PnL and total PnL, plus the same metrics on the common scored target
intersection. Flat returns are zero PnL and excluded from accuracy denominators.
PnL is Phase 6's percent unit long/short return without fees.

Phase 6 direction outcomes are gate-selected and may cover only part of the 500
hours; do not interpret them as performance over all hours. Reports before all
500 slots are recorded are exploratory/incomplete. No percentile selector is
implemented and no live decisions consume these reports.

## Dashboard

The navigation includes **Phase 5b**, with routes `/phase5b/`,
`/phase5b/data.json` and `/analyze/?phase=phase5b`. The page displays the fixed
500-slot window, status counts, actual-RV availability, latest 100 targets and
percentile comparisons on common scored targets and each method's available
scored targets. Reads use read-only SQLite connections; viewing a page never
runs migrations, predictions or evaluation. Missing DB/schema/init and an absent
Phase 6 schema produce an informative empty state.

The dashboard follow-up changes `app.py` and `ui.py`, adds `phase5b_views.py`
and its tests, and updates this document. These files are outside both the
Phase 6 frozen source manifest and the Phase 5b predictor source manifest.
An already initialized Phase 5b does **not** need reinitialization.

After pushing the dashboard commit, on the server:

```sh
cd /opt/btc-predict
git status --short --branch
git fetch origin phase5b
# Replace DASHBOARD_COMMIT with the reviewed follow-up commit, not the original init commit.
git cherry-pick DASHBOARD_COMMIT
.venv/bin/python -m unittest test_phase5b_views test_ui
# Confirm the actual web service name before running this command.
sudo systemctl restart btc-web.service
curl --fail http://127.0.0.1:8001/phase5b/data.json
```

Restart only the web application to load routes/navigation. Do not restart the
hourly prediction services or reset either experiment. If Phase 5b is not yet
initialized, follow the original initialization/timer steps first. On this
installation the expected execution account is `ubuntu`: confirm it against the
existing units, use `sudo -u ubuntu` and set `User=ubuntu` / `Group=ubuntu` in the
new observation service before installing it.
