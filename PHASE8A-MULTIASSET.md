# Phase 8a: 5銘柄の手動・実ニュース比較

対象はBTC-USD、ETH-USD、SOL-USD、XRP-USD、LINK-USD。米国株・実取引は対象外。
通常の`live_enabled=false`と定期予測停止を維持する。`phase8a.pilot`は明示実行した
単発バッチだけで4エンジン×5銘柄、最大20呼出し。モデル失敗・中断を自動再試行しない。

## ニュースの選別

8preの取得時刻・記事版を引き継ぎ、全銘柄で同じUTC cutoff、同じ適格snapshotを使用。
直前24時間の英語RSS見出し・概要のみ。モデルごとの検索・選別は行わない。
銘柄名またはティッカーの単語境界一致を直接関連とし、暗号資産市場・金融政策・
物価・雇用などの固定キーワードを市場共通とする。LINKは小文字の一般語linkを除外。
Ripple社のニュースもXRPの関連候補として扱うため、直接の価格材料とは限らない。
直接関連→市場共通、その中で新しい順。URLと正規化テキストの完全一致を除重。
意味的な関連性・因果性を保証する分類器ではない。選別版は
`asset-direct-common-newest-url-text-v2`。除外理由、関連区分、記事ID・版ID・URL・出典を保存。
最大5記事・2400bytesは維持。直接記事が0件でも市場共通の記事があればテストし、
`direct_news_count=0`を明示。全く関連記事がなければバッチ全体を呼び出し前に停止。

追加候補NewsBTCは[Press Policy](https://www.newsbtc.com/press/)がRSS利用を明示的に案内し、
[規約](https://www.newsbtc.com/terms-and-conditions/)第1節は素材固有の条件を優先する。
そのRSS許諾を根拠に、出典・リンク付きの供給済み見出し/概要の私的な比較研究に限定する。
AI利用を個別に明記したライセンスではない、という解釈上の限界をpolicyに記録する。
本文ページの収集、学習・微調整、公開転載は行わない。収集元追加は多様性や網羅性の保証ではない。
CoinDeskの従来の停止を維持し、AI利用を禁止するCointelegraph、保存を禁止するDecrypt等は採用しない。
RSS接続失敗を回避するための偽装やアクセス制限の迂回はしない。

## 予算と操作

通常のpredict/tickは価格未確認のKairosを拒否する。ユーザーの明示承認を受けた手動pilotだけ
`--allow-unpriced-kairos`で例外を許す。価格確認済みと偽装せず、費用はnullのまま保存する。
Kairosはgeneralのみ。confidenceと上昇確率は区別し、確率は未校正。
Astraはdefault、Claudeはstandard_onlyを明示し、高額優先枠を使わない。
3エンジン全15呼出しの保守的トークン料金上限を実行前に合算し、0.99ドル以下に制限する。
既に実施した接続テスト既知費用0.00509258ドルを含めても既知部分は1ドル未満。
Kairosは単価不明なので、この上限に含めた総額保証はできない。
各DBの予約額は内部会計値であり、Kairos実料金の上限ではない。

本番で秘密情報を表示せず、専用`.env.phase8a`をdotenvでプロセス環境へ読み込む。
シェルでsourceしない。通常設定にlive_enabled=trueを書き込まない。
cutoffは次の300秒境界。収集完了後の境界を選び、T〜T+60秒以内に明示実行する。

```sh
python -m phase8a.pilot run --config phase8a.config.json --news-db /opt/btc-news/news.db \
  --directory outputs/real-news-pilot-UNIQUE --cutoff UTC_EPOCH \
  --allow-unpriced-kairos --known-budget-usd 0.99
python -m phase8a.pilot report --directory outputs/real-news-pilot-UNIQUE
```

manifestは全銘柄の凍結入力・料金上限・承認例外をAPI前に永続化する。
同じディレクトリの再実行は中断後も禁止。別ディレクトリでの再試行は追加課金になるので、
人が原因・残予算を確認するまで行わない。自動的な別ディレクトリ作成ループは禁止。
APIキー・取得記事・生回答・DBをGitHubに置かない。

## 4時間後の価格突合

直後は予測回答の比較のみで、Accuracy/Brier/LogLossは未確定。
T+4時間+60秒以降、次の手動コマンドで価格を取得し評価する。有料モデルは呼ばない。

```sh
python -m phase8a.pilot evaluate --directory outputs/real-news-pilot-UNIQUE \
  --btc-candles-db /opt/btc-predict/candles.db --fetch-prices
```

BTCは既存の価格アーカイブを読み取る。他4銘柄はCoinbase Exchangeの公開読取APIから
必要な3本の確定5分足だけ取得し、銘柄付きの専用`asset-prices.sqlite3`へ初回値を保存。
後から取得するのは実現値だけで、予測入力には一切戻さない。価格欠損は補間しない。
元のBTC専用DBをETH等の評価に渡すと拒否する。既存価格サービスは変更しない。
参照はT直前の終値、paper entryはT+300の始値、終値はT+4h直前の終値。
銘柄別に評価し、5銘柄を独立な5標本と見なしてまとめない。
UTC 00/04/08/12/16/20時の既定非重複評価ルールは維持するため、その他の時刻の単発pilotは
参考評価のみとなり、正式非重複スコア・paper PnLの対象外。新たな定期予測は起動しない。
1回の比較から「BTCよりアルトコインが予測しやすい」という結論は出さない。

## テスト・配置

```sh
python -m unittest discover -s tests_phase8a -q
python -m unittest discover -s tests_news -q
```

銘柄別入力、LINK誤認、直接記事優先、共通snapshot、予算合算、再実行拒否、
未確認価格の例外範囲、銘柄違いの価格拒否、時点制約をテストする。
既存Phase 6/6b・webは変更しない。`/opt/btc-phase8a`だけのコード更新と、
`/opt/btc-news/news.config.json`への追加feed反映を行う。news timerの間隔は既存900秒を維持。
巻き戻す場合はニュース設定のバックアップとPhase8aソースのバックアップを戻す。
新しいDB・凍結結果は削除しない。予測timerは導入・起動しない。

2026-11-12検証: 本番と同じPython 3.12環境で全回帰246件成功。対象テスト68件成功。
手動実ニュースpilotは20/20回答成功。実測データは非公開のoutputsに保存する。
初期選別デモの合成見出しも新しい関連性ルールに合わせ、模擬予測・評価1件の成立を確認。

後続指示により毎時運転を承認。以後の運用は[PHASE8A-HOURLY.md](PHASE8A-HOURLY.md)を参照。上記の「定期予測は未開始」は単発テスト時点の記録。
