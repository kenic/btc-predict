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
The runner stops each model after 48 Phase 4 predictions.

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
