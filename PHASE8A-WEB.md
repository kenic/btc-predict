# Phase 8a Web dashboard

`/phase8a/` displays saved forecasts, outcomes and metrics for BTC/ETH/SOL/XRP/LINK.
`/phase8a/data.json` exposes the same allowlisted public projection. Navigation retains Phase 8pre.
The Web process imports only phase8a_views and shared presentation helpers; it never imports or
calls prediction, collection, evaluation, systemd or provider adapters. Opening/refreshing the page
cannot incur a model API call, create a database or change an experiment.

Default data directory: `/opt/btc-phase8a/outputs/hourly-20261112`. The existing Web service runs as
ubuntu, so no permissions on private outputs or API keys are broadened. The view reads only
`report.json`, `prediction-status.json`, and a selected run's `summary.json`.
Only fixed numeric/status fields are published. Configurations, provider raw output, requests,
input article text, private source paths and credentials are excluded from both HTML and JSON.
NewsBTC excerpts remain private; the page shows selected/direct article counts instead.

Query parameters: `symbol` from the five explicit USD pairs; optional `cutoff` must match a recorded
cutoff in the most recent 72 scheduled runs. No user-controlled file paths. Missing/corrupt root
reports yield a descriptive page and HTTP 503; invalid filters return 400. A missing run summary
is displayed as unavailable, not fabricated. Report age over five minutes shows a stale banner.
Status describes saved records and the configured enable flag, not a live systemd liveness claim.

All display times are JST. The page refreshes every 60 seconds. Forecast probability is uncalibrated;
50% displays as 五分五分 while stored scoring uses UP. Kairos confidence is never substituted for it.
Metrics show hourly descriptive pairs separately from four-hour non-overlapping primary pairs.
Zero evaluated pairs show no scores, rather than zero performance. Cross-asset results are not pooled.
The recent outcomes list may include the earlier manual pilot. Cost is the selected batch's known
five-asset estimate and excludes unknown Kairos charges. Per-run paper results are explicitly before
API costs; non-primary hours have no paper opportunities. No live trades.

Validation: fixture rendering, no write/no network behavior, HTML escaping and public-field
allowlisting, malformed/missing reports, invalid selectors, symlink rejection, Flask routes and
navigation regression. Production validation is performed on a checkout based on the actual Web
HEAD, preserving the Phase 8pre dashboard and all earlier features.

Deploy only the additive Web commit to `/opt/btc-predict` and restart `btc-web.service`. Do not copy
the experiment checkout wholesale or restart prediction/collection services. Rollback uses the
previous Web commit and a Web-only restart. Keep all experiment databases and outputs.
