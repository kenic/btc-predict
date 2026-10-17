# Phase 6b — extended replication / observation

Purpose: Extended replication / observation of Phase 6.
Start: Immediately after Phase 6 reaches its preregistered 96 accepted ACTIVE
 targets, their actuals are evaluated and the operator explicitly records closure.
Stop: cumulative accepted ACTIVE targets = 200 (104 additional after Phase 6).
Change from Phase 6: observation horizon only. No optional stopping.

Unchanged: single-shot ACTIVE volatility, ten-repeat modal ACTIVE, repeat
agreement >=8/10, all direction predictors and ensembles, logistic training
procedure (expanding strictly prior evaluated samples), GPT prompt/model settings,
next-hour target definition, metrics, and signed PnL (percentage return times
long/short sign, zero for flat, no fees). No selector/meta-selector.
Phase5b stays independent; JOIN on `phase6b_events.target =
phase5b_predictions.target_candle_time` using the existing hourly timestamps.

## Design and preservation

No existing tracked file is changed. In particular phase6.py, phase6_methods.py,
next_stages.py and every other original implementation-hashed source stay byte
identical. Phase6's saved config and stop=96 remain intact. Its scheduler keeps
running normally and stops creating targets at 96. Existing Phase6 dashboard
queries continue to describe only the original Phase6 results.

`phase6b.py start` takes the same `.next-stages.lock` as the existing scheduler.
It requires exactly 96 accepted Phase6 events, all actuals evaluated, no pending
gates, and no unfinished accepted calls/predictions. It verifies the original
preregistration/config/source hashes and a clean committed tracked checkout.
It inserts a closure record and the new start config in a single transaction.
No Phase6 record is updated. The closure stores the ordered 96 target timestamps
and a digest of their event/call/prediction rows, plus the original config digest.
Every 6b cycle rechecks this closure and both sets of frozen hashes before calls.
Repeated start validates and returns the existing marker; it never resets it.
No automatic start, backfill or replacement. Missing data remain missing.

6b stores observations exclusively in `phase6b_events`, `phase6b_calls` and
`phase6b_predictions`. `phase6b_config` retains the exact original config JSON,
with a new start timestamp/boundary and code SHA; `phase6b_manifest` explicitly
records the different horizon (104 additional, 200 cumulative), closure, manifest
version and new implementation hashes. These tables are created only on explicit
start. The original JSON's 96 stop describes the primary experiment; the manifest
and runtime ADDITIONAL=104 govern 6b. All observations in these tables belong to
6b, so no original rows need relabeling. Accepted ordinal 1 in 6b is cumulative97.

`phase6b_runtime.py` is a storage-isolated copy of four frozen runtime functions.
Tests compare their ASTs to Phase6, allowing only namespace, manifest validation,
and additional stop-limit substitutions. Prediction helpers, the GPT prompt,
gate, logistic implementation and ensemble calculations are imported directly
from original frozen sources. Training combines the existing legacy/Phase6
snapshot pool with past Phase6b snapshots, deduplicates targets, and uses the
same logistic cutoff/standardization/filtering. Evaluation writes only 6b tables.
The shared scheduler lock serializes all 6/6b starts/cycles across processes.
At 104 accepted new targets, prediction stops; evaluation continues to collect
final actuals. The timer can remain enabled for this purpose.

## Prepared against production (2026-10-17)

Host ubuntu@nepp, /opt/btc-predict: main at 3b3b114, two local commits beyond
remote main 661a068 (Phase5b implementation and dashboard); tracked tree clean.
Untracked plots and candles.db WAL/SHM were excluded. Phase6 count was65/96,
all65 evaluated; original frozen hashes matched. Phase6 start config hash:
448e17d05b20a0644a36c5be0ac0be4354053ab93edfadd977356fb7125683cf.
Existing btc-predict.timer runs at hh:05 and executes run_hourly.sh as ubuntu;
Phase5b's independent timer runs at hh:01:30. No production code/DB/systemd
changes or Phase6b start were performed during preparation.

## Operator transition — only after 96 and approval to deploy/start

Do not run the old scheduler manually as a check. Let its normal hourly cycle
finish the 96th actual evaluation (normally the following hour). The following
commands are a future operator runbook, not preparation-time actions. First
transfer/review the prepared phase6b branch/commit using the normal approved
Git workflow. In /opt/btc-predict verify status and remote refs; preserve local
commits and untracked files. The branch descends from production3b3b114, so only
a fast-forward is allowed; stop and review if the production branch has diverged.

```sh
cd /opt/btc-predict
git status --short --branch
git rev-parse HEAD
# After explicit approval and making the reviewed branch available locally:
flock .next-stages.lock sh -eu -c '
  .venv/bin/python -c '\''import sqlite3; src=sqlite3.connect("file:btc.db?mode=ro",uri=True); dst=sqlite3.connect("/tmp/btc-before-phase6b.db"); src.backup(dst); dst.close(); src.close()'\''
  git merge --ff-only phase6b
  .venv/bin/python -m unittest discover -q
'
.venv/bin/python phase6b.py status
# Must show phase6_accepted=96. start also requires all96 evaluated.
.venv/bin/python phase6b.py start > /tmp/phase6b-manifest.json
.venv/bin/python phase6b.py status
# Retain the SQLite backup, manifest, original Phase6 results and code commit.
# Only with explicit authorization to install/enable the prepared timer:
sudo install -m 0644 deploy/systemd/btc-phase6b.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/btc-phase6b.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btc-phase6b.timer
systemctl list-timers --all btc-phase6b.timer
```

The new timer runs at hh:01:45 UTC, with the same ten-minute live deadline.
`start` never makes provider calls. It records the next hourly target, so enabling
at another time cannot backfill prior targets; the first eligible target is the
next future boundary. Do not start the service manually to test it.
The new service uses ubuntu, matching the inspected production account.
No existing service/timer restart is necessary. Verify after the next boundary:

```sh
cd /opt/btc-predict
.venv/bin/python phase6b.py status
journalctl -u btc-phase6b.service --since today --no-pager
```

At cumulative200, wait until the last actual is evaluated, export/retain 6b
results and status, then disable the 6b timer if desired (separate approval).
Analyze primary96, additional104 and cumulative200 explicitly as distinct cohorts.
No deployment/start/stop based on interim performance (no optional stopping).
For a technical failure, preserve all rows; restore the committed frozen code
before resuming. Never restore an old DB over live observations. The original
Phase6 scheduler remains independent and bounded at96 throughout.
