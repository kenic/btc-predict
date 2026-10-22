# Phase 6c — Claude direction side observation

## Manifest

Version `claude-side-v1`, same initialization, calendar boundary, provider settings,
frozen sources and deployment procedure as PHASE5C.md. No optional stopping.
Independent calendar end is **2026-11-02 00:00:00 UTC exclusive**; it never extends
Phase 6's N=96 or Phase 6b's cumulative N=200. Any earlier end of those populations
simply leaves no more accepted targets for 6c; Phase 5c continues every hour.

Only an existing `gate='accepted'` event in `phase6_events` or `phase6b_events`
is eligible, at or after registration. Read and save that exact context and hash,
with cohort and target. Do not reconstruct snapshots, rerun volatility calls,
recalculate acceptance or read actuals into a prediction prompt. Missing 6b tables
before automatic handoff are normal; they are read on a later poll when present.
Accepted historical events missed by the side worker are recorded as missed,
never submitted after target+600. Returning after the deadline is marked late.

The exact GPT direction prompt is extracted from `phase6.direction_call`.
The same single context replacement (RV heading → direction heading) is applied.
Model settings: `claude-sonnet-4-6`, temperature 1.0, max_tokens 1024, thinking
explicitly disabled, no tools/system prompt/retries. See PHASE5C.md for details.

## Primary and repeats

- Slot 0 is the preregistered primary direction forecast.
- Slots 1–10 are ten **additional independent** stateless requests with the same
  model, prompt, context and sampling settings. No seed, consensus feedback,
  adaptive prompt or previous answer is supplied. Up to four concurrent calls.
- Primary and each repeat store UP/DOWN, p_up, p_down. As in GPT, UP when p_up>=.5;
  probabilities must be finite, within [0,1], and sum to 1 within 1e-6.
- Primary failure is never replaced by a successful repeat. All slots are claimed
  at most once, including failed/uncertain slots. Each request checks its live
  deadline immediately before dispatch. HTTP failures do not stop the RV branch.
- **Neither Claude nor repeat results are ever added to existing predictor sets,
  ensembles, ACTIVE gates, logistic training or any live decision.**

Read actual_direction, actual_return and evaluated_at from the same original
cohort/target, with matching snapshot hash, after the target closes. No new market
return definition: signed PnL is +actual_return for UP and -actual_return for DOWN,
percent without fees. Correct is direction equality; Brier is
`(p_up - I(actual_direction='UP'))^2`. FLAT: correct/Brier NULL and PnL zero.
These metrics are stored for every complete primary/repeat; failed/late calls
remain unscored. A missing original evaluation stays pending, even after expiry.

## Analysis

`claude_analysis.py` provides:

- GPT / Logistic / primary Claude common-target N, accuracy (with explicit
  denominator), Brier (with explicit denominator), mean and total signed PnL.
  Timestamp and snapshot must match, all three predictions must be complete and
  scored. Both per-cohort and pooled overlapping-population reports are emitted.
- Complete ten-repeat sets: modal direction, agreement=max(UP,DOWN)/10.
  A 5–5 tie has agreement .5 and no modal direction (abstention). Fewer than ten
  on-time successes have NULL agreement/modal; partial counts remain visible.
- Primary performance by complete-repeat agreement, plus modal accuracy and PnL
  for evaluated targets with a successful primary. Modal Brier is NULL: a vote
  share is not silently treated as a calibrated probability. This subset and
  its selection on successful calls are exploratory, not a new live gate.
- GPT/Claude same/different cases with cohort, timestamp, probabilities, directions,
  realized outcome and both PnLs; paired performance need not have Logistic present.

All analysis is read-only and retrospective. No performance threshold, report,
repeat agreement or disagreement case affects observation timing or selection.

## Validation and preparation state

Preparation uses a new local branch `phase5c-phase6c`, based on production
`75bd499`. Existing files remain unchanged. At read-only production inspection
on 2026-10-22, Phase 6 had 91 accepted events; Phase 6b had not initialized yet.
Phase 5b and Phase 6 registered source hashes matched. These counts are a snapshot,
not a deployment precondition; recheck on installation.

Tests cover exact GPT prompt parity, saved snapshot reuse, pending/rejected and
6b gates, independent failures, missing credentials, API response validation,
late/missed/expiry boundaries, concurrent claim idempotency, crash reservations,
source DB immutability, canonical RV/missing candles, FLAT outcomes, common-target
intersection, and repeat ties/incomplete sets. Real socket connections are blocked
in the test run. No provider call, push, deploy or service action was performed.

## Read-only dashboard follow-up

`/phase6c/`, `/phase6c/data.json` and `/analyze/?phase=phase6c` expose
accepted-target progress, primary forecasts, ten-repeat completion, modal
direction/agreement, and stored outcomes. Complete ten-repeat ties and
incomplete sets remain distinguishable. Comparison tables include common-target
GPT/Logistic/Claude performance by cohort, primary/modal performance by repeat
agreement, and GPT/Claude same/different performance with the latest 100 cases.

The dashboard imports the unchanged frozen claude_analysis report; no gate,
predictor, repeat, metric or stopping behavior is modified. Viewing a route
does not fetch market/provider data or write either database. Progress remains
visible if the original comparison database is missing or unavailable.
