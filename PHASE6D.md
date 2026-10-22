# Phase 6d — independent Claude ACTIVE detection and direction

## Manifest

- Version: `phase6d-v1`. New development branch `phase6d`, based on Opus
  preparation/UI commit `e9701046179b7d06ab733d10c62d6f9c00c706c6`.
- Purpose: observe Claude's own prospectively selected ACTIVE population.
  Phase 6c continues to observe direction on the GPT-selected population.
- Start: next UTC hour after explicit init. No model call occurs during init.
- Stop: **2026-11-02 00:00 UTC, exclusive**, matching the promotional-credit
  experiment window. Fixed consecutive calendar slots; no optional stopping,
  target replacement or extension after failures/outages. This is a separate
  calendar experiment, not Phase 6's N=96 or Phase 6b's cumulative N=200.
- Model: **claude-opus-5-5**; max_tokens=4096 (thinking plus text),
  thinking=adaptive, output_config.effort=medium; temperature/top_p/top_k omitted.
  API version 2023-06-01, direct Anthropic Messages endpoint, no retries,
  redirects, tools, system prompts, adaptive prompts or previous answers.
  Timeout up to 120s, capped by remaining live deadline. Four repeat workers.
- Provider configuration follows the already approved Opus side observation.
  Reference: https://platform.claude.com/docs/en/models/opus-5-5/migration-guide
- All targets are hourly UTC start timestamps. New requests and complete response
  eligibility must be within target+600s and strictly before calendar end.
  Late responses are retained and excluded from scoring and gating.
- Runtime, dependency versions, request parameters, prompt text, thresholds,
  source hashes, paths and Git commit are frozen before the first target.
  Existing init validates the original config and cannot reset its window.
  No pre-start-refreeze action is provided for 6d.

## Independent ACTIVE gate

Every hourly target receives a new Claude three-class RV forecast. Predict and
store p_quiet, p_normal, p_active and argmax class; the exact Phase 4 QUIET,
NORMAL, ACTIVE tie order is retained. Copy the original Phase 6 preregistered
volatility_config, after verifying its config hash. Do not recalibrate thresholds.
Use unchanged phase4_runner.instructions plus the exact GPT class-call JSON
instruction. Same semantic inputs/instructions; no assumption of identical
provider sampling or reasoning budgets.

Only a single-shot ACTIVE triggers **ten additional volatility repetitions**.
All ten must succeed on time. Accepted requires single ACTIVE, repeat modal
ACTIVE and >=8/10 ACTIVE votes, exactly the original Phase 6 gate function.
A non-ACTIVE single forecast is rejected without repeats or direction. Failed,
late or incomplete class/gate attempts stay unknown rather than becoming a
valid non-ACTIVE detection. Their coverage remains visible.

**Only Claude-accepted targets receive one new Claude direction forecast.**
The user selected this population explicitly. Direction repeats are not added
in 6d; the ten repeats here are volatility repetitions. The direction prompt
is the exact original GPT direction prompt, with its same heading replacement.
UP when p_up>=.5; finite [0,1] probabilities must sum to one within 1e-6.

No GPT class or GPT accepted decision is read by the runtime to authorize
6d predictions. GPT can reject while Claude accepts and predicts direction.
Phase 6/6b/6c predictor sets, gate decisions, repeats, ensembles, logistic fits,
stopping conditions, stored configs and frozen source files are unchanged.
6d outputs feed no other live experiment or trading/selector decision.

## Snapshot and actual semantics

Use the exact saved 5c context/hash when available, regardless of provider
success; otherwise use saved 5b context/hash. Neither source depends on GPT's
accepted gate. Within 5b's calendar window wait for its saved input; missing
input at deadline is a missed slot, not a reconstructed historical forecast.
Outside that window only, use the unchanged 5b/Phase 4 build_snapshot helper.
The saved context must pass Phase 6 alignment/extraction checks. All class,
repeat and direction requests for a target use the same text without outcomes.

Evaluation uses existing candles.db only, read-only, after target+3660.
RV uses the unchanged helper with thirteen consecutive 5m closes, including the
preceding close, in percent and nonannualized. Actual class uses the copied
frozen thresholds. Missing windows remain pending, without interpolation.
The target-hour close is the final 5m candle close at target+3300. Direction
return is 100*(target-hour close / saved snapshot reference close - 1).
This is the same target-hour-close/reference definition as Phase 6; deriving
it from the local 5m archive avoids another provider/network dependency.
Cross-experiment direction comparisons additionally require equal actual
returns/direction, so any archive-versus-1h-source differences are excluded.

Class scoring saves three-class accuracy/Brier and binary ACTIVE Brier for
complete class calls. Direction saves correct, binary Brier and signed PnL.
FLAT has NULL correct/Brier and zero PnL. Signed PnL is +return for UP and
-return for DOWN, percent without fees. No invented probabilities for the gate.
Evaluation and pending-actual recovery can continue after calendar end; no new
forecast requests occur then.

## Storage, isolation and restart

Separate **phase6d.db**, never the original or existing Claude DB. Three tables:

| Table | Key | Contents |
|---|---|---|
| phase6d_config | singleton | start/end, frozen config JSON/hash, Git SHA, read-only source paths |
| phase6d_events | target | status, input/hash/source/reference, single class, repeat modal/agreement/votes, gate/reason, actual RV/class/return/direction |
| phase6d_calls | target,kind,slot | class slot 0 plus 1–10; direction slot 0 only; statuses/timestamps, probabilities, returned model/raw response, safe error type, scores |

A separate phase6d.db.lock prevents concurrent 6d workers; no existing experiment
lock is acquired. Event and call keys atomically reserve attempts before API
work. Successful, failed, late and uncertain calls are never retried. Interrupted
events remain interrupted; later hours continue. Waiting input is polled only
within its original live window. Missed historical slots never cause calls.
Each network request occurs outside write transactions. SQLite busy timeout is
short; failure cannot propagate into existing workers.

ANTHROPIC_API_KEY comes from the already configured root-owned mode-0600
/etc/btc-predict/claude.env. No key values, exception messages or HTTP error
bodies/headers are saved. Raw responses use the existing credential-redacting
adapter. New DB and lock files are covered by existing *.db / *.db.* Git ignores.
Maximum new requests per target: 1 single + 10 volatility repeats + 1 direction
= **12**, only when single ACTIVE and accepted; non-ACTIVE targets use one.

## Analysis and dashboard

```sh
.venv/bin/python phase6d_analysis.py --db phase6d.db > /tmp/phase6d-analysis.json
```

- Own single-shot ACTIVE confusion matrix, recall/precision, accuracy, binary
  ACTIVE Brier; three-class accuracy/Brier.
- Own completed valid accepted-gate detection metrics and missing/failed coverage.
- Direction N, accuracy/Brier denominators, mean/total signed PnL on Claude's
  accepted population.
- GPT/Claude single-shot detection on common timestamp AND byte-identical input.
- Gate overlap: both accepted, Claude-only, GPT-only, neither, only when both
  gates are complete and valid. Failed gates do not count as valid negatives.
- GPT / Logistic / Claude 6c / Claude 6d direction on the common scored intersection,
  with matching snapshot and realized outcome; not pooled across different gates.
- GPT/Claude 6d same/different direction performance on matched scored targets.

`/phase6d/`, `/phase6d/data.json`, `/analyze/?phase=phase6d` and navigation expose
these read-only reports. Missing/uninitialized DB does not get created by viewing
a page. Prompts, input context, raw provider responses and keys are not public.
All comparisons are exploratory; results do not change the observation window.

## Deployment and explicit start

After reviewing the additive commit and testing in an isolated checkout, inspect
production Git state, original/Claude frozen hashes and unit account. Make SQLite
backup-API backups before applying changes. Fast-forward the reviewed branch;
never reset/reinitialize existing observations. Only app.py/ui.py and new 6d
files/document are changed; the 5c/6c frozen hash file list is untouched.
Restart only btc-web.service to load the new dashboard.

Install the new unit definitions (does not start 6d):

```sh
sudo install -m 0644 deploy/systemd/btc-phase6d.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/btc-phase6d.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

When explicitly starting 6d, initialize as the deployment user and enable only
its independent timer. Init fixes the next UTC hour, without making API calls:

```sh
cd /opt/btc-predict
sudo -u ubuntu .venv/bin/python phase6d.py init
sudo systemctl enable --now btc-phase6d.timer
.venv/bin/python phase6d.py status
journalctl -u btc-phase6d.service --no-pager -n 50
```

The timer polls every minute at **:45 UTC**, offset from 5c/6c's :15 poll.
Persistent=false, no automatic retrospective requests. Deployment need not
restart/stop any existing observation or collector. Leave the timer for final
actual recovery after the end; prediction calls cease automatically.
