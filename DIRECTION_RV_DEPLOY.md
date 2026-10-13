# Phase 1–3 retrospective direction / realized volatility

Branch: phase4r-phase5. No migrations or experiment-state changes. Only btc-web
needs restarting. No Phase 6 predictions or scaffold are introduced.

The /analyze/ direction view (all/phase1/phase2/phase3) adds fixed Phase 4 RV
regimes and matched-cohort ALL and NON-ACTIVE summaries. Each model and phase
reports N, correct, accuracy, binary Brier, mean/median absolute actual return,
return N, and mean RV. ACTIVE minus NON-ACTIVE accuracy is displayed with N.
RV uses volatility.realized_volatility unchanged: closes timestamped t-300
through t+3300, twelve close-to-close log returns, multiplied by 100.
QUIET excludes its upper boundary; ACTIVE includes its lower boundary.
No interpolation, annualization, forward prediction, or threshold refitting.

Eligible rows have evaluated_at, actual UP/DOWN, Phase 1–3, openai/jev and valid
binary probabilities. Correct uses stored values with the existing UP tie
fallback. FLAT and pending outcomes are excluded. Missing windows are excluded
only from the RV analysis and counted per model/phase. Missing actual_return
reduces Return N only. These are exploratory future-outcome-conditioned subsets;
calibration overlaps historical experiments, and shared hours are correlated.
Accuracy differences do not establish statistical significance or trading value.

## Deployment

Run on the production host. Confirm the service directory is the Git checkout
and that its code working tree is clean before updating. Preserve production DB,
.env, calibration files, enable markers and running timers.

```sh
systemctl show btc-web.service -p WorkingDirectory -p ExecStart
cd "$(systemctl show btc-web.service -p WorkingDirectory --value)"
git status --short --branch
git rev-parse HEAD  # record for rollback
git switch phase4r-phase5
git pull --ff-only origin phase4r-phase5
.venv/bin/python -m unittest discover -v
sudo systemctl restart btc-web.service
systemctl is-active btc-web.service
curl --fail --silent --show-error https://btc.kenic.jp/analyze/ | rg 'Direction accuracy by realized volatility'
```

No collector/worker/timer restarts and no database migration are required.
The tracked Phase 4 calibration archive is used first. If Missing RV is nonzero,
this optional explicit command queries only uncovered historical target windows
through the existing Coinbase fetch_candles utility and saves an ignored
`direction_history_5m.json` cache. It never updates predictions or thresholds.
Run as the checkout owner, so btc-web can read the resulting file.

```sh
.venv/bin/python direction_volatility.py --fill-gaps --db btc.db > /tmp/direction-rv-report.json
```

Inspect failed_windows and report.missing in that report. Any failed/incomplete
window remains excluded; conflicting candles are never overwritten. No web
restart is needed after filling the cache. Open /analyze/ and all three phase
filters to compare ACTIVE/non-ACTIVE and coverage. The report JSON also contains
the exact aggregate metrics used by the page. The optional cache is historical
market data only and may be removed to return to calibration-archive coverage.

Local validation: complete unittest discovery, isolated database immutability,
exact 13-close window, preceding-close omission, invalid close, threshold boundary,
grouping, Brier, return magnitude and filtered route rendering. Local archive has
4,033 5m candles. The available legacy DB backup contains only two rows and no
Phase metadata; no full Phase 1–3 production DB is available locally. Consequently
actual historical accuracy and missing counts must be obtained on the server;
no GPT/Jev empirical conclusion is claimed from fixtures.

Rollback: restore the recorded previous code revision using the normal deployment
procedure and restart btc-web only; retain all experiment data and running jobs.
