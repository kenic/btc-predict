# Phase 4R instability analysis deployment

Read-only analysis/UI update on `phase4r-phase5`. No migration, sampling,
model calls, scheduler changes, or backfill. Counts come from frozen source rows.
GPT and Jev have independent eligible N; paired cohorts use timestamp intersection.
A missing Jev source is never reconstructed. Actual production results are shown
when the dashboard reads the server DB; this checkout has no current experiment DB.

Stability, agreement buckets and ensemble changes require exactly 10 valid completed
responses. Incomplete/invalid snapshots remain in eligible N and progress counts,
but not stability denominators. Flip denominator includes all completed responses,
including partial snapshots. Original and repeat argmax are derived from stored
probabilities with the existing QUIET/NORMAL/ACTIVE tie order. Ensemble argmax
uses mean probabilities, not majority voting. Scoring requires an actual label and
10 valid repeats; single and ensemble use identical rows. Buckets contain modal
vote counts 10, 8–9, 6–7, and 4–5. Every percentage field uses 0–100 units.
Instability/outcome comparisons are descriptive, without statistical significance
or causality claims. Paired stability uses common complete timestamps; paired
performance uses common evaluated timestamps and reports its separate N.

## Server steps

The production checkout directory is not recorded in the repository. Replace
`/ACTUAL/PRODUCTION/btc-predict` below with the existing checkout. The documented
web service is `btc-web.service`; verify its working directory before restarting.
If the working tree is dirty, preserve/reconcile local changes before merging.

```sh
cd /ACTUAL/PRODUCTION/btc-predict
git status --short --branch
git rev-parse HEAD  # save for rollback
git fetch origin phase4r-phase5
git switch phase4r-phase5
git merge --ff-only origin/phase4r-phase5
.venv/bin/python -m unittest test_repeated_analysis test_phase4 test_phase4_views test_next_stages test_ui -q
.venv/bin/python -m compileall -q next_stage_views.py repeated_analysis.py repeat_views.py app.py
systemctl show btc-web.service -p WorkingDirectory -p ExecStart
systemctl cat btc-web.service
sudo systemctl restart btc-web.service
systemctl is-active btc-web.service
curl --fail --silent --show-error https://btc.kenic.jp/repeated/ -o /tmp/btc-repeated.html
curl --fail --silent --show-error 'https://btc.kenic.jp/analyze/?phase=phase4r' -o /tmp/btc-repeated-analysis.html
rg 'class-specific instability|agreement vs performance|Paired complete N' /tmp/btc-repeated.html /tmp/btc-repeated-analysis.html
curl --fail --silent --show-error https://btc.kenic.jp/next-stages/data.json -o /tmp/btc-next-stages.json
```

Check model eligible N against the source DB (expected GPT 96, Jev 95), complete
N against valid-repeat progress, and paired N against the common timestamps.
Only restart the dashboard service. Leave workers, collectors, timers, database,
configuration and markers untouched. Rollback to the recorded previous revision
with the normal code rollback process and restart the same dashboard service.
