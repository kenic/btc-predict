# Phase 7 — frozen selector, 60-day prospective paper observation

User authorization: train on Phase 6 + 6b and conduct Phase 7; 60 calendar days,
API forecasting authorized, paper execution only. No exchange orders.

## Start and stop

`phase7.py arm` records an immutable waiting configuration without API calls.
The minute timer waits until all original 96 + extended 104 accepted outcomes
are available. It snapshots all source rows and quotes, trains, freezes the
model and starts at the NEXT UTC hour. End is start + 60*86400, exclusive.
No performance-based extension, early termination, replacement hours or retry.
After the fixed end only evaluation continues. Arming twice never resets data.

Original Phase 6, 6b, 5b, Claude studies and collection continue unchanged.
Source btc.db, market.db and candles.db are opened read-only. Separate phase7.db
stores configuration, full training snapshot, predictions, calls and quotes.
Frozen source hashes and Python package versions are checked every cycle.
Existing source code/config hashes are verified before arming; source Phase6/6b
config hashes must still match at model freeze. No source outcomes are rewritten.

## Training, without treating development performance as evidence

Selector actions: GPT, Order Flow, Logistic Regression, cash. Historical input
predictions are the original saved prospective predictions, NOT predictions
from a model refitted on their own labels. All 200 accepted targets are required.
Missing historic model predictions are no-trade. All 200 historical entry and
exit quotes must exist within 15 seconds; a missing target blocks freezing.

One-split tree or constant: features |return_1h|, |buy_ratio-0.5|, |sma_gap|,
|GPT p_up-0.5|; thresholds training 25/50/75 percentiles, >=20 rows per leaf.
13 candidate structures. Expanding validation folds (80 train/40 validation,
120/40, 160/40), purging any train target whose hour or available label/exit quote
is not strictly before the validation start. Candidate invalid in any fold is
excluded instead of scored on a smaller validation set. Select maximum pooled
validation mean net bp, deterministic first-candidate tie order. Refit threshold
and actions on all 200. Freeze training-best fixed action separately.
Objective: saved executable bid/ask return minus additional 5bp roundtrip.
All of 6/6b is development data; no claim of fresh OOS evidence from its scores.

Frozen Logistic fits all available labeled Phase6/6b snapshots (accepted AND
rejected), strict outcome availability and closed-hour cutoff, deduplicated by
target, using original standardized L2 logistic implementation, eight features,
400 iterations, lr=.1, penalty=.01, >=100 rows and >=10 examples per class.
No legacy labels and no Phase7 labels are added. Coefficients/scaling/IDs/hash
are saved. Historic Logistic used expanding training; frozen Phase7 is an
explicit deployment change, not asserted to be numerically identical.

## Live inputs, gate and models

Use original build_snapshot with target-aligned completed candles and market
features. Retain exact text/hash. New single GPT volatility call; if ACTIVE,
10 concurrent repeats; accepted iff all valid, modal ACTIVE and >=8/10 votes.
Same original prompt, model identifier and frozen volatility thresholds.
No cached calls or post-hoc reconstructed snapshots. Maximum 12 GPT calls/hour
(1 single + 10 repeats + 1 accepted direction); rejected non-ACTIVE uses one.
API cost is variable and not included in unit-notional trading bp.

Original direction rules and ensembles, one GPT direction and frozen Logistic
are evaluated on the SAME snapshot. Selector and training-fixed copy their
chosen prediction; no new GPT call for selector. Cash/unavailable chosen model
means no trade; missing selector feature means cash. No unregistered fallback.
Base predictors and both original ensembles are saved for comparison.

Live prediction starts only from target+105 through target+480 seconds. Each
network call starts no later than +480, timeout120, max_retries=0. Responses
completed after +600 are stored late and excluded. Entire common decision must
finish by +600. One reservation per target, all calls reserved before sending;
interrupted, failed or late attempts cannot be retried. Independent file lock,
minute :45 UTC timer, Persistent=false. Missed hours remain missing.

## Paper execution and scoring

All models share common decision completion time. Entry is first stored Coinbase
BTC-USD best bid/ask at/after ready, <=15s later. Exit first quote at/after hour
end, <=15s later. Long: exit_bid / entry_ask -1; short: 1-exit_ask / entry_bid.
Per-roundtrip 5bp ADDITIONAL fee/slippage plus spread already in quotes; report
0/2/5/10/20bp sensitivity. Unit notional, additive return, no leverage, funding,
borrow fees, actual market orders or guaranteed fills. Coinbase cash market
shorting capability and available size not established. This is paper research.

Missing entry quote excludes that accepted hour from executable set for ALL
methods. Missing exit remains unknown. Failures, no-trade, late and missing
coverage are displayed. Every executable accepted hour is shared across models;
unavailable/cash predictions are zero PnL. Direction accuracy separately uses
original reference to hour-end close and excludes FLAT/absent predictions.
Local saved 5m candle close at target+3300 supplies direction outcomes.

## Fixed primary comparison and conclusion

Primary: mean net bp difference Selector minus Order Flow on common accepted
executable opportunities. At end only: UTC calendar-day circular moving-block
bootstrap (3 days, 10000 draws, seed72026). Include zero-opportunity calendar
days in resampling; weight by accepted executable counts. Diagnostics blocks
1 and7 days, secondary comparisons/GPT/LR/ensembles exploratory.

Supports improvement only if lower95% CI >0 and mean difference >=1bp,
with >=100 executable opportunities, >=30 days having executable opportunities,
>=95% of 1440 planned gates known/complete, and all accepted execution outcomes
complete. Missing execution outcomes withhold a positive verdict. Insufficient
coverage or failure to pass is NOT an instruction to extend the window.
Report max drawdown and model failure coverage as secondary risk measures.
Intervals are not shown as significance tests during the observation period.

## Operations

Run targeted tests and entire existing test suite in isolated checkout with
production virtualenv; no tests against live data. Commit reviewed additive
files; fast-forward production only if it remains on expected clean HEAD.
Install independent phase7 service/timer; arm; enable --now timer. Restart only
btc-web to expose /phase7/ and /phase7/data.json. Never reset existing studies.

Read-only status: `.venv/bin/python phase7.py status`.
