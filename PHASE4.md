# Phase 4 — next-hour realized volatility

Implemented in the local copy at `/Users/iwa/Documents/src/btc-predict`. No remote
server, live timer, or deployment was accessed. The copied final Phase 3 target
starts 2026-09-30 14:00 UTC and completes at 15:00 UTC (2026-10-01 00:00 JST).
The two pending predictions must remain pending until then.

## Frozen target

RV is `100 * sqrt(sum(log(C_i / C_(i-1))^2))`: twelve consecutive 5m
close-to-close log returns including the preceding close. Units are percent,
not annualized. Missing/invalid candles cause evaluation to remain pending;
no interpolation or replacement with zero returns is performed.

- QUIET: RV < 0.22742560978910695%
- NORMAL: 0.22742560978910695% <= RV < 0.37066415640951506%
- ACTIVE: RV >= 0.37066415640951506%

Calibration uses 336 complete nonoverlapping hours from 2026-09-16 14:00 UTC
through 2026-09-30 14:00 UTC, exclusive. Quantiles are 1/3 and 2/3 using linear
interpolation. Each historical class contains 112 hours. The majority baseline
therefore resolves the tie to QUIET. The history, its SHA-256, source, time
bounds, quantile method and frozen thresholds are saved in
`phase4_history_5m.json`, `phase4_config.json`, and the append-only
`phase4_configs` table. Never regenerate this config during Phase 4.

## Closing and starting

On the actual machine running the experiment, stop its prediction scheduler
before closing Phase 3. This local copy cannot stop a remote timer. Preserve
any newer server data; do not replace a live DB with this stale copied DB.
Use the existing server Python environment with its original dependencies.

After the final target completes, in this repo run:

```sh
.venv/bin/python prepare_phase4.py --close-phase3
.venv/bin/python enable_phase4.py
```

Closing evaluates only outstanding Phase 3 rows with the existing UP/DOWN
rules, requires exactly 48 matched and evaluated hours for both predictors,
saves `backups/phase3-boundary/closed-btc.db`, and creates the annotated
`phase3-complete` tag pointing to the saved pre-change code snapshot.
It is safe to retry closure. Enabling is separate and makes no model call.
Restart the hourly scheduler only after closure and enablement succeed.
The first new prediction must be at or after the frozen calibration end;
more than 10 minutes after its hourly cutoff is rejected to avoid hindsight.
The original design stops each model after 48 Phase 4 predictions; the
continuation policy below preserves that cohort and adds a second 48.

`predict_gpt.py` and `predict_jev.py` now enter the Phase 4 runner. The original
Phase 3 functions remain for reference, but the normal entry points cannot
create further direction predictions. Technical calculations, timeframe
history lengths, cutoff filtering, microstructure validation and snapshot
composition reuse Phase 3 code. Only the target description changes.
The persistence baseline is computed from the preceding complete hour and
saved at prediction time. Jev confidence is stored separately from class
probabilities. Probabilities must be finite, in [0,1] and sum to one within
1e-6; ties resolve QUIET, NORMAL, ACTIVE in that order.

`evaluate.py` evaluates the new table and the original legacy table separately.
Phase 4 uses per-row frozen configuration, never the current file. New
predictions live in `volatility_predictions`; legacy rows/schema are preserved.

## Views and scoring

- `/`: Phase 4 probability forecasts, outcomes, scores and fixed baselines.
- `/analyze/?phase=phase4`: multiclass scores, confusion matrices and
  per-class probability calibration, including the 0–50% probability range.
- `/direction/`: existing Phase 1–3 dashboard and virtual trading simulation.
- `/analyze/` and phase1/phase2/phase3 filters: existing direction analysis.

Accuracy uses argmax. Multiclass Brier sums squared errors over the three
classes, without dividing by three (range 0–2; uniform baseline 2/3).
Majority and previous-hour persistence baselines use exactly each model's
evaluated hours. No Phase 4 virtual trading is performed.

## Backups and rollback

`backups/phase3-boundary/btc.db` and `market.db` are SQLite-consistent initial
backups. `backups/phase3-boundary/app.py` preserves the existing uncommitted
dashboard changes. `phase3-boundary-code` is a Git snapshot containing those
changes without changing the current branch or index.

To pause predictions remove `phase4.enabled`. For full code rollback inspect
`git diff phase3-boundary-code` and restore the tracked files from that tag.
New Phase 4 files can remain dormant; do not delete prior experiment data.
Restore a backed-up DB only with all writers stopped and after separately
saving the current DB. The additive schema can be left in place when rolling
back code.

## Verification

Run `python -m unittest test_phase4 -v`, compile all `.py` files, and
`bash -n run_hourly.sh`. Dashboard routes are checked with Flask's test client.
Model API calls are not made while preparing; real model integration requires
the configured production SDK versions and a running market collector.

## Phase 4a + 4b continuation deployment

The original `prediction_limit: 48` and frozen config hash remain unchanged.
A separate continuation policy allows 96 saved predictions **per predictor**:
Phase 4a is ordinal 1–48, Phase 4b is 49–96. Combined scores are descriptive;
4b is an additional replication decided after observing 4a interim results.
Ordinals include pending evaluations. They use each model's chronological
saved targets, matching the original per-model limit. If a model misses an
hour, it can finish later than the other model; no late backfill is allowed.
Neither model's success advances the other model's count. The cadence remains
unchanged. The 96th prediction still needs evaluation on the following run;
keep the hourly scheduler running after prediction collection ends.

`phase4_segments` is an additive read-only SQL view. No existing row or column
is changed. Both pages also derive the same cohorts without requiring the
view, supporting an unmigrated existing DB. The transaction in `save_prediction`
serializes count/append/insert and rejects the 97th record, duplicate or earlier
targets, and a different frozen config. API calls and evaluation rules are unchanged.

Apply on the server that already runs Phase 4, between hourly runs. Do not run
`prepare_phase4.py`, regenerate calibration, re-enable Phase 4, replace databases,
or deploy any local backup. No remote restart has been performed by this change.

1. Record the current commit, working tree status, and the existing scheduler
   and dashboard service names. The repository must have no uncommitted code
   changes. If it does, preserve and review them before updating. In the existing
   deployment directory, use its Python environment:

   ```sh
   git status --short
   git rev-parse HEAD
   git fetch origin
   git merge --ff-only origin/phase4-continuation
   .venv/bin/python -m unittest test_phase4 -v
   bash -n run_hourly.sh
   ```

2. After the active hourly job finishes and before the next :05 invocation:

   ```sh
   .venv/bin/python migrate_phase4_continuation.py
   .venv/bin/python -c "from phase4 import load_config, prediction_limit; c=load_config(require_enabled=True); print('frozen limit',c['prediction_limit'],'total per model',prediction_limit(c))"
   git diff 0e9001f -- phase4_config.json phase4_history_5m.json
   ```

   The migration first validates the original config and existing counts,
   creates a SQLite-consistent backup under `backups/phase4-continuation-*`,
   and only adds the view. Repeating it is safe and makes another backup.
   Expected limits are `48` and `96`; the frozen-file diff must be empty.
   A model already at 48 resumes at the next on-time hourly target automatically.
   Missed hours before deployment remain missing.

3. The hourly script starts new Python processes each run, so the runner needs
   no restart and the timer schedule must stay unchanged. Reload the existing
   Gunicorn/dashboard service using its actual configured service name:

   ```sh
   sudo systemctl reload YOUR_EXISTING_DASHBOARD_SERVICE
   ```

   If that service has no reload action, restart that dashboard service alone.
   Service names are not stored in this repository; substitute the verified
   name from the server rather than guessing it. Do not restart collectors or
   change the prediction timer. Open `/` and `/analyze/?phase=phase4`: both must
   show Phase 4a, Phase 4b and Combined; the analysis page has separate confusion
   and calibration tables for each. Inspect the existing hourly logs after the
   next :05 run and check the cohort view:

   ```sh
   .venv/bin/python -c "import sqlite3; from phase4 import DB_PATH; c=sqlite3.connect(DB_PATH.resolve().as_uri()+'?mode=ro',uri=True); print(c.execute('SELECT predictor,cohort,count(*),sum(evaluated_at IS NOT NULL) FROM phase4_segments GROUP BY predictor,cohort').fetchall())"
   ```

Rollback: remove `phase4.enabled` to pause predictions if needed, preserve the
current database, then return code to the recorded prior commit and reload the
dashboard. Leave the additive view and all 4b data in place. Original code will
stop predicting once a model has at least 48 records; do not delete new records
to make it run again. Never restore a stale backup over the live DB.
