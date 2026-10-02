# Phase 4 cohort dashboard update

Both `/` and `/analyze/?phase=phase4` now have a six-row comparison table
(4a, 4b, combined × OpenAI, Jev), including saved, pending and evaluated counts,
Accuracy and multiclass Brier. Each cohort has a linked section with model and
baseline scores; analysis adds separate confusion and calibration tables.

The existing `phase4_segments` view is used when present. Otherwise the existing
`segment_rows` helper derives ordinals from chronological targets independently
for each predictor, including pending predictions. No migration is needed for
this dashboard update. Neither route writes to the database or calls model APIs.
Phase 1–3 handlers, frozen configuration, prediction cadence and runner are unchanged.

## Apply and restart

The patch is based on the supplied repository's `bea9191` continuation code.
No remote server was accessed. This checkout has no live `btc.db`, and the
actual production directory and dashboard service name are not recorded here.
Use the verified production directory and unit name in the commands below.
The patch changes only `phase4_views.py` and adds tests and this document.

1. Copy `phase4-dashboard.patch` to the production host using your existing
   transfer method. In the existing production repository, check that the
   working tree is clean and the installed code has `phase4.segment_rows` and
   the Phase 4 route dispatch in `app.py`. If there are local code changes,
   preserve and reconcile them before applying the patch.

   ```sh
   cd /ACTUAL/PRODUCTION/btc-predict
   git status --short
   git rev-parse HEAD
   .venv/bin/python -c 'import phase4; import app; print(phase4.segment_rows); print(app.app.url_map)'
   git apply --check /ACTUAL/UPLOAD/phase4-dashboard.patch
   cp phase4_views.py phase4_views.py.pre-cohort-dashboard
   git apply /ACTUAL/UPLOAD/phase4-dashboard.patch
   .venv/bin/python -m unittest test_phase4 test_phase4_views -v
   .venv/bin/python -m compileall -q app.py phase4.py phase4_views.py test_phase4_views.py
   bash -n run_hourly.sh
   ```

2. Verify the dashboard unit from the server's existing service configuration:

   ```sh
   systemctl list-units --type=service --all | rg -i 'btc|gunicorn'
   systemctl cat ACTUAL_DASHBOARD.service
   systemctl show ACTUAL_DASHBOARD.service -p WorkingDirectory -p ExecStart -p CanReload
   ```

   Confirm that it runs this production checkout. If `CanReload=yes`, run:

   ```sh
   sudo systemctl reload ACTUAL_DASHBOARD.service
   ```

   Otherwise run:

   ```sh
   sudo systemctl restart ACTUAL_DASHBOARD.service
   ```

   Then verify:

   ```sh
   systemctl is-active ACTUAL_DASHBOARD.service
   curl --fail --silent --show-error https://btc.kenic.jp/ -o /tmp/btc-phase4-dashboard.html
   curl --fail --silent --show-error 'https://btc.kenic.jp/analyze/?phase=phase4' -o /tmp/btc-phase4-analysis.html
   rg 'Cohort comparison|section id="phase4a"|section id="phase4b"|section id="combined"' /tmp/btc-phase4-dashboard.html /tmp/btc-phase4-analysis.html
   ```

   Open both pages and confirm that each predictor's saved and evaluated 4a + 4b
   counts equal combined counts. Pending rows must not contribute to scores.
   With zero evaluated 4b rows, its scores show `—`. Analysis must have confusion
   and calibration tables under each of the three sections.

No scheduler or collector restart is required. Do not copy databases, frozen
configuration or historical calibration files from this checkout. Do not run
closure, enablement or continuation migration scripts for this display update.

## Rollback

Restore `phase4_views.py.pre-cohort-dashboard` to `phase4_views.py`, then reload
or restart the same dashboard unit as above. Leave all database data in place.
The new test and documentation files can remain; they have no runtime effect.
