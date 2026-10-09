# Phase 6 actual volatility visibility

Read-only reporting uses saved Phase 4/5 actual_rv first, then complete exact
5m JSON archives beside the code or the selected btc.db. Known archives are
phase4_history_5m.json and direction_history_5m.json. Each target requires 13
closes from target-300 through target+3300. assign_rv calls the unchanged Phase 4
realized_volatility helper; classify uses the saved Phase 6 config's frozen
Phase 4 thresholds. Conflicting or invalid closes invalidate their timestamp,
not unrelated hours. No archive is written or fetched by reporting.

Investigation of branch phase6 at 1e60126:
- Phase 6 evaluate saves one completed 1h candle in target_raw_json, plus return
  and direction, for both accepted and rejected events. It does not save 5m data.
- Phase 4/5 evaluation saves RV values, not the raw fetched 5m evaluation window.
  Phase 4R reuses saved prediction snapshots and repeats; it adds no candle store.
- The original dashboard reads only the two JSON archives beside its code;
  it does not resolve archives beside a separately located DB. A conflicting
  candle anywhere in those archives aborts all fallback RV assignments.
- predict_gpt and phase4_runner keep fetched 5m arrays in memory and save rounded
  context text. That text is not an exact raw candle archive and is not used.
- collect_trades and collect_orderbook persist trades and book samples in market.db;
  no candle collector/table exists in this branch. Trades are not reconstructed
  into provider candles.
- Inspected local /Users/iwa/Documents/src/btc-predict/btc.db has predictions,
  phase4_configs and volatility_predictions (zero Phase 4 rows), no Phase 5/6
  tables. market.db has trades and orderbook_samples, no candles/evaluations.
  The local Phase 4 archive has 4,033 rows, timestamp range 1789566900–1790776500.
  No runtime Phase 6 database was available locally. Therefore specific production
  rejected-window coverage has not been verified; deployment cannot recover
  closes that were never saved. No live server was accessed.

Validation: 60 unittest tests pass, including accepted/rejected exact archive
loading, canonical RV and frozen classification, missing candle, independent
conflicts, pending/unavailable, read-only DB bytes, unchanged implementation
hashes, no network calls or prediction reruns, and dashboard/JSON route rendering.
Existing UI tests emit ResourceWarnings for static file handles; no failures.

Deployment (operator only, existing Phase 6 checkout):

```sh
cd /opt/btc-predict
git status --short --branch
git pull --ff-only origin phase6
sudo systemctl restart btc-web.service
systemctl is-active btc-web.service
curl --fail https://btc.kenic.jp/direction-active/
curl --fail 'https://btc.kenic.jp/analyze/?phase=phase6'
```

Pull + btc-web restart suffices for this reporting-only change. No migration,
start command, evaluation/backfill command, gap-fill command, collector restart,
or scheduler restart is needed. Keep existing DBs, archives, calibration and
start markers. No frozen implementation-hashed files are changed, and there is
no change to Phase 5, gate, direction methods, stop rules or experiment counts.
