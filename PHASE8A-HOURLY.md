# Phase 8a 毎時予測と4時間後の自動照合

2026-11-12、ユーザーがBTC/ETH/SOL/XRP/LINK×4エンジンの毎時運転と4時間後の
答え合わせを明示承認。従来の「定期予測は開始しない」はこの指示で解除。
さらに料金を気にせず継続する指示を受領。無料トークンの適用やKairosの単価は
推測せず、使用量・既知料金の推定・不明費用を区別して保存する。実取引は一切しない。

## 運転

- `btc-phase8a.timer`: 毎時00分10秒UTC（JSTも毎正時10秒）。T=正時として予測する。
- `btc-phase8a-evaluate.timer`: 毎分40秒。T+4時間+60秒以降、確定価格を取得して照合。
- 初回予定は2026-11-12 13:00 JST。価格取得が間に合えば17:01:40頃に自動照合。
- 12:25 JSTの単発テストも対象。16:26:40以降に照合可能。
- 価格が欠ける場合はpendingのまま、次回の無料価格取得で再確認。有料モデルを再呼出ししない。
- サーバ停止中の予測は遡って作らない（Persistent=false、T+60秒超の開始を拒否）。
- ソースや実験設定が途中で変更されると予測を拒否する。新しい実験ディレクトリを明示設定し、
  旧ディレクトリのrunsをadditional_batchesへ加えて既存予測の評価を継続する。

`phase8a.schedule.json`のenabledとallow_unpriced_kairosが定期運転の専用許可。
従来`phase8a.config.json`のlive_enabled=falseは旧CLIの不用意な直接実行を防ぐため維持。
サービスはdotenvのinterpolate=Falseで必要な4キーだけ読み込み、値をログに出さない。
評価サービスはキーを読み込まず、モデルAPIを一切呼ばない。

## 二重課金・異常時の制限

1時刻×1銘柄×1モデルは最大1回。5銘柄を同じsnapshotで固定後、20件を並行実行。
グローバル台帳へ保守的費用と20件を先に予約し、クラッシュ後も再送しない。
APIエラー時の自動再試行・代替モデルへの切り替えはない。
ニュース不足・古い収集元・事前検証失敗もその時刻を見送りとして記録する。

通常480件/UTC日以内、過去30日14,400件以内（Kairosは各時刻5件）。
既知3エンジンの料金上限予約は25ドル/UTC日、過去30日750ドル。
これは通常運転を妨げない異常時の上限で、想定請求額ではない。毎回の入力と出力上限から
計算する既知料金予約は1バッチ0.99ドル以下。無料枠の消化で予測を省略しない。
Kairos分の費用は不明であり、これらの金額は4エンジン合計の保証ではない。
上限に達した時刻はAPIを呼ばず、評価処理は継続。制限は専用schedule設定で変更できる。

## 結果

`/opt/btc-phase8a/outputs/hourly-20261112/`:

- `schedule.sqlite3`: 時刻ごとの予約・実行・見送り状態。
- `runs/<UTC epoch>/`: 各時刻の凍結入力、銘柄別DB、生回答、使用量・費用、照合済み価格。
- `report.json`: 銘柄別Accuracy/Brier/LogLoss、全時刻参考評価、4時間非重複評価、
  各回答の的中/外れ、予測確率と実現価格。
- `prediction-status.json`: 直近の予測処理結果。

銘柄間は相関があるので一括の独立標本として扱わない。毎時の4時間予測は重複するため、
UTC00/04/08/12/16/20時の結果を主評価として別集計する。
銘柄別DBの既存reportに閾値・費用込みpaper評価も保存される。Kairos費用不明のため
確定した総費用差引き損益とは呼ばない。直接記事の不足は件数として記録する。

## 確認・停止

```sh
systemctl list-timers btc-phase8a.timer btc-phase8a-evaluate.timer
journalctl -u btc-phase8a.service -u btc-phase8a-evaluate.service --since today
/opt/btc-predict/.venv/bin/python -m phase8a.scheduled check \
  --schedule /opt/btc-phase8a/phase8a.schedule.json --env-file /opt/btc-phase8a/.env.phase8a
```

新規予測だけ止める場合、`sudo systemctl disable --now btc-phase8a.timer`とし、
scheduleのenabled=falseにする。評価timerは継続させて保存済み予測を照合する。
両方止める場合はevaluate.timerもdisableする。DBと成果物は削除しない。

配置は`/opt/btc-phase8a`に限定。既存BTC予測・Web・価格・ニュース収集サービスは変更しない。
追加で有効になっている収集元を発見しても、本タスクでは既存の許可設定を上書きせず、
予測のallowlistも勝手に広げない。


## 2026-11-12: 初回見送りとWAL読取の修正

13:00 JSTはpreflight_skipped、0呼出し。ニュース収集は13:00:01までに成功し、
予測は13:00:10に即時見送り。キー・設定の更新はなく、後から同じ入力の事前チェックは成功した。
当時の例外詳細は保存されておらず、過去の例外を断定的に復元することはできない。

同じsystemd保護設定で、補助ファイルが存在しないWAL方式のDBを開くと
`sqlite3.OperationalError: unable to open database file`を再現した。
SQLiteは読取り時でも-wal/-shmの作成が必要な場合があり、収集側の最終接続終了時には
補助ファイルが削除され得る。確認済みの挙動は[公式仕様](https://sqlite.org/wal.html#read_only_databases)に一致する。
補助ファイルが残った状態だけでの起動前確認では、この配置上の不具合を検出できなかった。

既存本番ディレクトリ全体の書込み許可を広げる案は自動承認審査で拒否されたため、採用しない。
代わりに既存収集サービスのExecStartPostからSQLite backup APIを使い、
`/opt/btc-phase8a/inputs/{news,candles}.db`へ整合した読取り用コピーを作る。
コピーはDELETE journal、0440、完成後にatomic replace。元DBはmode=ro/query_onlyで接続し、
元のWAL方式・データ・収集コードは変更しない。元DBへのimmutable=1やraw file copyは使わない。
収集サービスへの追加書込先は専用inputsディレクトリだけ。
予測/評価サービスには既存本番領域の書込権限を与えず、inputs自体もReadOnlyPathsにする。
ニュースの実取得時刻・記事版と価格の取得時刻はコピー内でもそのままなので、時点制約を維持する。

配置時はdeploy/systemdのbtc-news-phase8a-export.confとbtc-candles-phase8a-export.confを
各収集unitのservice.d/phase8a-export.confへ置き、inputsをubuntu所有・0700で準備する。
既存ExecStartを置き換えない。収集後にコピーが存在することを確認してからschedule.jsonの
news_db/btc_candles_dbをコピーへ向ける。予測Pythonコードと凍結contractは変更しない。
13:00の見送りは保持し、後追い予測はしない。出力失敗時は前回のコピーを保持する。
ニュースコピーが古ければ既存stale判定で見送り、価格が不足していれば照合をpendingとする。

回帰確認: 未チェックポイントのWAL内容の反映、元DBが不変、書込拒否、補助ファイル不要、
読取中のatomic replacement、失敗時に前回コピーを保持することをテスト。
