# Frozen Phase 4R / Phase 5 design and deployment

Based on local branch `phase4-continuation`. No production access, production DB,
or service changes were performed. This checkout has no live `btc.db` or `market.db`.
Observed production counts must be verified on deployment; 96 GPT / 95 Jev is
expected, not substituted for actual source counts.

## Experiment

Phase 4a/4b thresholds, configuration, rows, model calls and market composition
remain unchanged. `build_snapshot` extracts the existing input assembly verbatim.
The scheduler still calls GPT then Jev until the completed target hour of GPT's
96th original prediction. This defines the common Phase 4b end even when Jev has
one missing prediction. It does not wait for a 96th Jev prediction on an extra
hour. Do not keep independent old GPT/Jev scheduling jobs enabled after deploying
this scheduler, or they could extend Jev beyond that common boundary.

At that boundary an immutable-by-convention source manifest copies all original
GPT and Jev predictions at or before the boundary, including each saved context,
source ID, timestamp, probabilities, model version and frozen config. No missing
Jev prediction is created. Expected 4R workload: GPT 96 × 10 = 960 and Jev
95 × 10 = 950, total 1910 successful samples. Actual model-specific n and common
paired timestamp n are reported. Each eligible original prediction gets exactly
10 independently recorded repeats, numbers 1–10. Replay uses its exact saved
context and original Phase 4 instructions, JSON task or Jev Choice criteria, and
requests its recorded model version. A retired/unavailable version fails visibly;
never substitute a newer model silently. The SDK's Jev response must expose
`model_dump_json()` as in the installed typesafe-sdk version; verify against the
production SDK before starting billable sampling. OpenAI and Jev SDK automatic retries are
disabled; provider-internal execution/retry behavior is outside local guarantees.
Jev retry configuration follows the [official SDK usage guide](https://docs.typesafe.ai/sdk/python/usage).

Each completed repeat records predictor, source ID, target, repeat number,
probabilities, argmax (ties: QUIET then NORMAL then ACTIVE), returned model,
start/end timestamps and raw response. Per-snapshot summaries: mean probabilities,
sample SD (n−1), max−min range and modal argmax agreement fraction. Brier scores
use only full 10-repeat ensembles and original evaluated targets. The report
compares original single-shot and ensemble mean Brier on the same scored rows.
Dispersion is the sum of class sample variances; descriptive Pearson correlations
against ensemble Brier and original 0/1 errors are reported; undefined correlations
remain null. Paired scores use the common evaluated timestamp intersection.
Partial ensembles appear as progress, not scores. Original Phase 4 evaluation
continues through the existing evaluator; no historical prediction fields are
changed by the new stages.

After all manifest repeats are complete, a durable Phase 5 marker records the
next hourly boundary. Phase 5 receives future live snapshots using exactly the
Phase 4 input construction: 1h/15m/5m technicals, order book and aggressive flow.
Only GPT runs. The target/output becomes a continuous `predicted_rv` JSON number.
Finite, non-negative values including zero are accepted; positive values have no
upper limit and are never clipped, winsorized or rounded in storage. Booleans,
strings, negative numbers, NaN and infinities fail validation. Failed attempts
are auditable and never silently converted into predictions. They are not part
of the 96 successful predictions; later valid hourly cycles continue until
exactly 96 are saved. Saved targets are not retried or backfilled. Predictions
more than ten minutes after their target boundary are refused.

RV is non-annualized percent:
`100 * sqrt(sum(log(C_i / C_(i-1))**2))`, twelve consecutive 5m returns with
the preceding close included. Evaluation directly reuses Phase 4's function.
Missing, conflicting, non-finite or non-positive candles leave evaluation pending;
no interpolation or zero-fill. MAE, RMSE and bias = mean(predicted − actual) use
completed target rows. The previous-hour RV baseline uses those identical rows.
The metrics helper accepts a prediction key so HAR-RV can be added later; it is
not included now. No optional stopping or interim design changes.

Routes: `/next-stages/`, `/analyze/?phase=phase4r`, `/analyze/?phase=phase5`,
and `/next-stages/data.json` (full diagnostics and predicted-vs-actual data).
The existing Phase 1–4 views remain available. Dashboard requests are read-only
and never issue API calls or migrations.

## Restart safety and uncertain API calls

New tables only: `experiment_transitions`, `phase4r_sources`, `phase4r_runs`,
`phase5_predictions`. Migration is idempotent; historical tables are untouched.
SQLite transactions persist transition markers and reserve calls before sending.
A file lock makes the scheduler and optional repeat worker mutually exclusive.
Completed repeat keys are always skipped. Each invocation processes up to 20
missing repeat keys; work resumes from remaining keys on the next invocation.

There is no atomic transaction spanning SQLite and a remote API. A crash after
the provider executes but before the response is saved leaves a `started` row;
API/validation failures leave a `failed` row. For 4R, either stops further
sampling rather than risk duplicate remote calls. An uncertain Phase 5 `started`
row also blocks subsequent prediction calls. Known failed Phase 5 attempts stay
visible and later hours may proceed. These cases require inspecting logs and
provider response history. Restore the verified original response into its
reserved row (validate probabilities/RV, record model/raw response/completed_at,
then set status complete). Do not delete reservations or automatically retry an
uncertain call. If the provider confirms no execution, a deliberate operator
retry can be arranged, preserving the original audit record. Without provider
reconciliation, exactly-once remote execution cannot honestly be guaranteed.
No automatic duplicate call is made for any persisted key.

## Deployment (user-operated)

1. Verify the actual production directory and hourly scheduler/dashboard unit
   names. Stop or pause the hourly scheduler briefly, leaving collectors running.
   Preserve any production code changes before applying this commit-ready diff.
   Do not copy local DB, calibration JSON, enable markers, `.env` or backups.

   ```sh
   cd /ACTUAL/PRODUCTION/btc-predict
   git status --short --branch
   git rev-parse HEAD
   .venv/bin/python -c 'import sqlite3; c=sqlite3.connect("btc.db"); c.backup(sqlite3.connect("btc.db.before-next-stages"))'
   git apply --check /ACTUAL/UPLOAD/next-stages.patch
   git apply /ACTUAL/UPLOAD/next-stages.patch
   .venv/bin/python -m unittest test_phase4 test_phase4_views test_next_stages -v
   .venv/bin/python -m compileall -q app.py phase4_runner.py phase4_views.py next_stages.py next_stage_views.py test_next_stages.py
   bash -n run_hourly.sh
   .venv/bin/python -c 'from next_stages import migrate; migrate()'
   .venv/bin/python -c 'import sqlite3; c=sqlite3.connect("btc.db"); print(c.execute("SELECT predictor,count(*),max(target_candle_time) FROM volatility_predictions GROUP BY predictor").fetchall())'
   ```

2. Keep the existing hourly scheduler pointing to `run_hourly.sh`; resume it.
   Remove duplicate jobs that directly run `predict_gpt.py` / `predict_jev.py`.
   For faster 4R completion, optionally add the following user crontab entry using
   the verified directory (it does not run Phase 4 or Phase 5 predictions):

   ```cron
   * * * * * cd /ACTUAL/PRODUCTION/btc-predict && .venv/bin/python next_stages.py --repeats-only >> next-stages-worker.log 2>&1
   ```

   Without the worker, the hourly scheduler processes 20 repeats per invocation.
   Budget for 1910 successful calls and monitor failures. Do not run the new
   scheduler manually during Phase 4 unless you intend a live prediction call.

3. Discover and verify the actual dashboard service, then restart it:

   ```sh
   systemctl list-units --type=service --all | rg -i 'btc|gunicorn'
   systemctl cat ACTUAL_DASHBOARD.service
   sudo systemctl restart ACTUAL_DASHBOARD.service
   systemctl is-active ACTUAL_DASHBOARD.service
   curl --fail https://btc.kenic.jp/next-stages/
   curl --fail 'https://btc.kenic.jp/analyze/?phase=phase4r'
   curl --fail https://btc.kenic.jp/next-stages/data.json
   ```

   Verify model n, paired n, saved repeat counts, persisted boundaries, failed
   rows, Phase 5 N and matching model/baseline evaluation N. No local tests make
   real model calls. Actual model compatibility and production service wiring
   require production verification by the operator.

Rollback: pause scheduler/worker first. Restore previous code and restart the
verified dashboard unit. Keep all additive tables. Do not reactivate the old
Phase 4 predictors after the common boundary: that would extend Jev's experiment.

## Approved recovery exception — 2026-10-05

The user approved exactly one replacement call for Jev source 77, repeat 8,
whose original attempt failed with `Probabilities must sum to one`.
This source has ten valid samples plus one failed attempt; the exception is
reported in `retry_exceptions` on the dashboard/JSON output. The original failed
row is archived verbatim in the additive `phase4r_retry_audit` table before its
repeat slot is authorized for replacement. No successful repeat is replayed.
Running recovery again does not grant a second replacement. New failures still
halt for review. The original failure's raw response was not captured by the
old code and cannot be reconstructed. The updated adapter retains raw provider
responses and returned model before validating probabilities; it does not relax
validation or normalize outputs.

After updating server code to this commit, run from `/opt/btc-predict`:

```sh
.venv/bin/python -m unittest test_phase4 test_phase4_views test_next_stages -q
.venv/bin/python -u recover_4r.py
```

The recovery waits for the shared worker lock, audits and authorizes only the
specified failed case, then processes remaining repeats sequentially. Keep SSH
connected, or run inside your existing terminal session manager. Once complete,
Phase 5 is enabled for the next valid hourly cycle; recovery does not itself
issue Phase 5 predictions. Restart `btc-web.service` to show retry audit data.
