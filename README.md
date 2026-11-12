# BTC Predictor — GPT vs Jev

A small experiment comparing **GPT-5.6 Sol** and **Jev** on hourly Bitcoin price direction prediction.

Every hour, both models receive the same BTC-USD market snapshot based on technical indicators and predict:

- UP or DOWN
- Probability of each direction

Predictions are evaluated using accuracy and Brier score.

The site also includes a virtual trading simulation starting with ¥10,000, where position size is determined by prediction confidence.

Live experiment: https://btc.kenic.jp/

This is an experimental project, not financial advice.

---

# BTC Predictor — GPT vs Jev

**GPT-5.6 Sol** と **Jev** に、Bitcoin の1時間後の騰落方向を予測させる小さな実験です。

毎時、両モデルに同じBTC-USDのテクニカル指標を与え、

- UP / DOWN
- それぞれの確率

を予測させます。

結果は的中率とBrier scoreで評価します。

さらに、初期資金1万円から、予測確率に応じて仮想的に売買した場合の資産額もシミュレーションしています。

実験サイト: https://btc.kenic.jp/

実験目的のプロジェクトであり、投資助言ではありません。

## Phase 4

Next-hour realized volatility classification is implemented; see [PHASE4.md](PHASE4.md) for frozen thresholds, closure/start guards, views and rollback. Phase 1–3 direction data remains intact.

## Phase 4R / Phase 5

Additive replay, continuous RV regression, audit markers and separate dashboard routes.
See [NEXT_STAGES.md](NEXT_STAGES.md) for the frozen design, restart safety,
API uncertainty handling and user-operated deployment/restart instructions.

## Generic 5m candle archive

Independent, future-only completed BTC/USD collection into `candles.db`, with
six-candle short-gap recovery and no interpolation/historical automatic backfill.
Phase 6 actual RV uses the archive through the unchanged Phase 4 canonical helper.
See [CANDLE_COLLECTOR.md](CANDLE_COLLECTOR.md) for schema, timing, tests and the
operator-installed systemd service/timer. Older missing hours remain unavailable.

## Phase 8a: common-news four-engine comparison

See [PHASE8A.md](PHASE8A.md) for the current audit, offline demo, API setup,
frozen evaluation contract and deployment procedure. The manual pilot supports BTC, ETH, SOL, XRP and LINK (USD pairs),
paper only, and paid calls are disabled by default. Keys are supplied separately.
Phase 8pre collection and its remaining coverage limits are described in
[PHASE8PRE.md](PHASE8PRE.md).

Manual real-news five-asset test and follow-up evaluation: [PHASE8A-MULTIASSET.md](PHASE8A-MULTIASSET.md). The subsequently authorized hourly service is documented below.

Hourly five-asset predictions and automatic four-hour evaluation: [PHASE8A-HOURLY.md](PHASE8A-HOURLY.md). The 2026-11-12 user instruction authorizes this scheduled deployment; example settings remain disabled.
## Phase 8pre news dashboard

The read-only [Phase 8pre page](https://btc.kenic.jp/phase8pre/) shows collection
health, polling coverage and the news known at a chosen cutoff. CoinDesk RSS,
FRB and BLS headlines are displayed with acquisition history and source attribution.
See [NEWS_DASHBOARD.md](NEWS_DASHBOARD.md) for query semantics, permissions,
testing, the independent news DB, deployment and rollback.
