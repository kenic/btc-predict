# Shared BTC Predictor UI deployment

This update is based on the latest `phase4r-phase5` branch. Only presentation,
route aliases, page caching, tests and documentation change. Experiment modules,
DB schema, state, API adapters, timers and model calculations are unchanged.
Existing module imports remain supported. Direction rendering stays in app.py;
volatility_views.py, repeat_views.py and regression_views.py provide semantic
entry points while the original modules retain their metric implementations.

`/` continues to show the existing current volatility experiment. `/volatility/`
is its semantic alias. `/next-stages/` retains the combined 4R/5 dashboard.
`/repeated/` and `/regression/` are dedicated views; all previous analysis query
URLs and `/next-stages/data.json` remain available. Analysis has a shared selector
for all direction phases, volatility, repeated sampling and regression.

## Apply on the server

Use the actual production checkout path. These commands assume Phase 4R/5 has
already been deployed and its migrations performed. If production predates
Phase 4R/5, follow NEXT_STAGES.md first; this UI update itself does not migrate.
Check that the working tree is clean before pulling and reconcile any local code
changes. Never replace production DBs, .env files or calibration/enable markers
with files from a development checkout.

```sh
cd /ACTUAL/PRODUCTION/btc-predict
git status --short --branch
git rev-parse HEAD  # record this revision for rollback
git fetch origin phase4r-phase5
git switch phase4r-phase5
git merge --ff-only origin/phase4r-phase5
.venv/bin/python -m unittest test_phase4 test_phase4_views test_next_stages test_ui -v
.venv/bin/python -m compileall -q app.py ui.py phase4_views.py volatility_views.py next_stage_views.py repeat_views.py regression_views.py
systemctl cat btc-web.service
systemctl show btc-web.service -p WorkingDirectory -p ExecStart
sudo systemctl restart btc-web.service
systemctl is-active btc-web.service
```

Verify that btc-web.service uses this checkout before restarting; substitute the
actual dashboard unit if it has another name. **Only btc-web needs restarting**
for this UI update. No collector, worker, timer or hourly scheduler restart or
pause is required, and no migration command is needed.

```sh
for path in / /direction/ /volatility/ /repeated/ /regression/ /next-stages/ /analyze/ '/analyze/?phase=phase4' '/analyze/?phase=phase4r' '/analyze/?phase=phase5'; do
    curl --fail --silent --show-error "https://btc.kenic.jp${path}" | rg 'Main navigation'
done
curl --fail --silent --show-error https://btc.kenic.jp/static/style.css > /dev/null
curl --fail --silent --show-error https://btc.kenic.jp/next-stages/data.json > /dev/null
```

Check desktop and mobile layouts, model/cohort counts and regression baseline N
against the prior dashboard. Local verification covers 21 tests, isolated DB
immutability, all HTML routes, active navigation, shared CSS/favicon, analysis
selectors, populated later-stage cards, escaping and existing metric regressions.
Production data and service wiring have not been accessed or verified.

Rollback: restore the recorded previous revision using your normal code rollback
process and restart btc-web. Keep all experiment data and running jobs intact.
