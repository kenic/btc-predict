# Phase 8pre2 — technical and executable-price archive

Independent reusable market-input infrastructure extracted from Phase 8c.
No model calls, forecasts, trade-ID continuity gate or exchange orders.
Only BTC-USD and XRP-USD. Existing 8c archives and services are not rewritten.

- `phase8pre2.technical`: Phase 6-family RSI14, MACD, SMA20/50, Bollinger position,
  volume ratio and returns on 200 confirmed 5m/15m/1h bars; 12 recent 5m OHLCV bars.
  Public REST collection every 30s after each collection finishes. Each first
  observation is immutable. A bar must be closed + 60s before receipt. Arbitrary
  event cutoffs select the latest bar actually observed by that cutoff, with
  complete 200-bar history and staleness bounded by interval + 180s.
- `phase8pre2.market`: bid/ask public ticker snapshots for both assets in parallel,
  a 2s wait after each poll. Request start and response receipt times are retained.
  This is polling, not a claim of exchange tick-exact executions or guaranteed fills.
- Separate `technical.sqlite3` and `quotes.sqlite3` under
  `/opt/btc-phase8pre2/data`; no credentials needed. Quotes include raw public response.
- Forecasters read these archives only. Later ingestion never becomes available
  retrospectively. Old market/trade collectors remain untouched.

Units: `btc-phase8pre2-technical.service`, `btc-phase8pre2-quotes.service`.
Health is exposed on `/phase8d/`. Do not delete archives while experiments reference
bar hashes or quote IDs. No automatic retention purge is enabled initially.

Standalone warm-up: `python -m phase8pre2.cli once --directory PATH`.
The continuous processes use `technical` and `quotes` in place of `once`.
