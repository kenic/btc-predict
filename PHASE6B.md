# Phase 6b — automatic extended replication / observation

Purpose: Extended replication / observation of Phase 6.
Start: the first hourly target after Phase 6 reaches 96 accepted ACTIVE targets.
Stop: cumulative accepted ACTIVE targets = 200 (104 additional after Phase 6).
Change from Phase 6: observation horizon only. No optional stopping.

Unchanged: single-shot ACTIVE volatility, ten-repeat modal ACTIVE, repeat
agreement >=8/10, direction predictors and ensembles, expanding strictly prior
evaluated logistic training samples, GPT prompt/model settings, next-hour target,
metrics and signed PnL (percent return times direction sign, flat zero, no fees).
No selector/meta-selector. Phase 5b remains independent; later JOIN using
`phase6b_events.target = phase5b_predictions.target_candle_time`.

## Automatic handoff and preservation

Install/enable the prepared timer BEFORE Phase 6 reaches 96, after deployment
approval. It runs at hh:01:45 UTC and takes the existing `.next-stages.lock`.
While Phase 6 has fewer than96 accepted targets it returns without provider calls,
DB schema changes or start markers. Existing btc-predict.timer runs at hh:05;
its frozen Phase 6 runner still stops at 96, as originally preregistered.

On the next hourly cycle after 96, the new runner automatically records membership
closure and starts 6b for that hourly target. Manual `start` is unnecessary and
Phase 6 evaluation completion is NOT a prerequisite. Example: Phase 6 accepts its
96th target at H:05; at H+1:01:45 the timer starts the next target in 6b, even if
Phase 6's last actual will only be evaluated by its normal H+1:05 cycle. Additional
accepted gates are numbered1..104 within 6b, equivalent to cumulative97..200.
A rejected gate is still saved as6b but does not increment the accepted count.
Missing calls, hours and outputs are not backfilled or replaced.

The Phase 6 config and stop=96 remain byte-identical. No frozen implementation
source is changed (phase6.py, phase6_methods.py, next_stages.py, phase4_runner.py,
volatility.py, predict_gpt.py, indicators.py, microstructure.py). Both source and
original config hashes must match. app.py, phase6_views.py and ui.py only change
read-only presentation and are outside the original frozen hash set.

`phase6b_manifest` v2 freezes the ordered96 target timestamps, their input/gate/
call/prediction fields, the original config record, and all outcomes already
completed at handoff. Only previously pending outcome fields may subsequently
be populated by the ORIGINAL Phase 6 evaluator. The6b runner never updates any
Phase 6 table. Once all 96 evaluations are complete, a single final result digest
is appended to `phase6b_primary_final`. Every cycle validates both the membership
snapshot and previously completed outcomes, and validates the final digest once
sealed. Thus6b observations do not delay or redefine the formal primary 96 result.

`phase6b_config` stores the exact original config JSON, new start metadata and
code SHA. The JSON's96 stop belongs to the primary experiment; the 6b manifest
and ADDITIONAL=104 govern the extension. New source hashes, including this
manifest documentation and timer definitions, freeze at handoff. A clean committed
tracked checkout is required. Repeated starts validate the existing marker and
never reset it. The unstarted original v1 preparation is superseded by v2; do not
reset a started v1 experiment or attempt to silently migrate its saved manifest.

`phase6b_runtime.py` isolates events/calls/predictions in dedicated 6b tables.
AST parity tests permit only table namespace, validation and horizon substitutions
in the four copied runtime functions. Gate, GPT call, rules, ensembles and
logistic are imported from the existing frozen code. Training combines legacy/
Phase 6 snapshots and6b snapshots, preserving deduplication, strict cutoff,
standardization and fitting. Prediction still precedes evaluation. At additional 104,
new prediction stops; later timer ticks continue final6b evaluations.
A technical interruption fails closed without retry/replacement or performance-
based changes to the stopping rule.

## Dashboard

- `/direction-active/` and `/direction-active/data.json`: primary Phase 6 only.
- `/phase6b/` and `/phase6b/data.json`: additional6b results only, accepted/104,
  cumulative accepted/200, and primary final-seal status.
- `/analyze/?phase=phase6b`:6b method metrics and paired comparisons.
- Main navigation and analysis filters link to6b; both direction pages link to
  each other. Before handoff the 6b page displays waiting status and primary count.

The6b metrics use its own 104 limit, not the 96 in the inherited original JSON.
Retrospective detectors that query only primary Phase 6 are not mislabeled as6b.
All pages use read-only SQLite connections and never start/migrate experiments,
fetch market data or make provider calls. Original and additional cohorts remain
separately labeled; the cumulative number is a count, not a pooled score.

## Production preflight and approved deployment

Preparation inspected ubuntu@nepp:/opt/btc-predict: main 3b3b114, two local commits
beyond origin/main 661a068; tracked tree clean, original frozen hashes matching,
65 accepted and 65 evaluated, no 6b tables or installed6b timer. Untracked plots,
DB/WAL/SHM, scratch and backup files are excluded from commits. Preparation only
updates the phase6b Git branch; it does NOT check it out in production, push,
change production DB, restart services or enable timers.

After explicit approval, apply the reviewed branch BEFORE 96 so transition needs
no operator action at 96. Recheck current HEAD, status, hashes, counts and unit
account. If production has advanced/diverged, stop and review; never force/reset.
Back up SQLite with its backup API, not a raw copy of a live WAL DB. Example:

```sh
cd /opt/btc-predict
git status --short --branch
git rev-parse HEAD
flock .next-stages.lock sh -eu -c '
  .venv/bin/python -c '\''import sqlite3; src=sqlite3.connect("file:btc.db?mode=ro",uri=True); dst=sqlite3.connect("/tmp/btc-before-phase6b-auto.db"); src.backup(dst); dst.close(); src.close()'\''
  git merge --ff-only phase6b
  .venv/bin/python -m unittest discover -q
'
.venv/bin/python phase6b.py status
sudo install -m 0644 deploy/systemd/btc-phase6b.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/btc-phase6b.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btc-phase6b.timer
sudo systemctl restart btc-web.service
systemctl list-timers --all btc-phase6b.timer
curl --fail https://btc.kenic.jp/phase6b/data.json
curl --fail https://btc.kenic.jp/phase6b/
```

No manual start or scheduler invocation is needed. Below96 the timer only waits.
No existing collector, predictor or Phase 5b timer restart is needed; the web
restart loads the read-only UI changes. Preserve the backup and code commit.
After 96, verify the automatic start and first saved6b target:

```sh
cd /opt/btc-predict
.venv/bin/python phase6b.py status
journalctl -u btc-phase6b.service --since today --no-pager
```

The timer uses Persistent=false and the original ten-minute deadline: delays or
outages do not cause retrospective predictions. The start boundary remains the
hour after the 96th target, so missing scheduled hours remain visible. When
cumulative 200 is reached, allow the last actual to complete, retain both cohorts
and final manifests, then optionally disable only the 6b timer with authorization.
For failure recovery preserve all rows; restore compatible committed code before
resuming. Never restore an old DB over live observations or regenerate frozen
config/hashes. No optional stopping based on interim performance.
