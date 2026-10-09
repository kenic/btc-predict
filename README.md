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
