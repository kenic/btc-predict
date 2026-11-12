# Phase 8a — 暗号資産の共通ニュース比較（実取引なし）

現在の毎時運転: [PHASE8A-HOURLY.md](PHASE8A-HOURLY.md)。

2026-11-12の追加対応: [5銘柄の単発テストと運用手順](PHASE8A-MULTIASSET.md)。以下の初期調査値は当時の記録。

## 現物の調査結果（2026-11-12 JST）

今回の開発元は本番と同じ `d5dca83`。GitHub main `661a068` から13コミット
先行しているため、古いmainへ直接実装せず、前回の本番互換検証コピーから
`phase8a-news-comparison` ブランチを作成した。本番のHEADはSSHで再確認。
本番tracked変更なし。既存の未追跡プロット・WALファイルは保持している。
該当するAGENTS.mdは見つからなかった。

8preは `/opt/btc-news` に独立配備され、15分ごとのFRB・BLS 3種の収集は
正常（service成功、有効4feedのstale=false、gap/rejected=0）。読み取り確認時は
56記事・56版、直近24時間の適格記事は0件だった。収集が正常でも、予測に
十分なニュースがあるという意味ではない。CoinDeskは既存方針どおり無効。
暗号資産ニュース源、本文全文、認証付きニュースAPIは未実装・未検証。
8preのRSSタイトル/要約、実取得時刻、append-only版履歴、履歴snapshotは利用する。
未取得の過去記事を公開時刻だけでバックフィルしない。
既存8preの間隔検証は600〜900秒に限定されていたため、今回の開発コピーでは
整数600〜3600秒へ拡張した。既存900秒の設定・履歴形式は維持。これは本番未配備。

Phase 6はACTIVEゲート後の1時間方向予測、96 accepted時刻。6bは同じ仕様の
104追加時刻。両者は凍結済みで、予測時刻・共通snapshot・失敗履歴を保持する。
その方針を引き継ぐが、今回はゲートなし、4時間、ニュースのみ、費用込みという
別実験。Phase 6/6bの確定ソース、DB、サービスには変更しない。
既存 `candle_store.py` の5分価格アーカイブの検証と価格源を再利用する。
既存Jevは `TypeSafeClient.system_one` / `Choice` / `jev-latest`。
既存GPTは `gpt-5.6-sol` であり、Astraの既存利用実績と誤認しない。

## 実装の範囲と動作確認

標準ライブラリだけでニュース選別、保存、模擬4エンジン、評価、レポートが動く。
Jevの実呼び出しだけ既存 `typesafe-sdk==0.7.0` が必要（本番のPython 3.12環境に存在）。
新たな注文API、取引所認証、ウォレット、実資金処理はない。

```sh
python3 -m unittest tests_news.test_news tests_phase8a.test_phase8a -q
python3 -m phase8a.cli check
python3 -m phase8a.cli demo --directory scratch/my-phase8a-demo
python3 -m phase8a.cli report --db scratch/my-phase8a-demo/phase8a.db
```

demoは新しいディレクトリを要求し、合成RSS・合成価格・模擬回答で一巡する。
レポートのOFFLINE表示とDBのmodeを固定し、実測成績と混ぜない。
既存の実ニュースを読み取る無料確認は次のとおり。空入力なら全エンジン見送り。

```sh
python3 -m phase8a.cli predict --db scratch/offline.db --news-db /opt/btc-news/news.db
```

`--as-of` はタイムゾーン付きISO 8601のみ。UTCまたは `+09:00` を指定。
UTC時刻をUnix秒で保存。未来・非整列時刻・タイムゾーンなしを拒否する。
過去の模擬処理は可能だが、実APIによる過去時刻の予測は拒否する。

## 固定する比較条件

BTC/ETH/SOL/XRP/LINKのUSDペアを許可。通常設定は`interval_seconds=3600`、`horizon_seconds=14400`。
銘柄ごとに別DBを使用し、価格源を分離する。手動pilotのみ300秒境界で1回実行できる。

時刻Tの直前24時間に公開され、かつTまでに保存された英語ニュースを8preから読む。
まずT時点の最新記事版を選ぶため、後日修正された記事を過去へ混入させない。
8preがmodel_inputを許可した版だけを使用。明示allowlistと銘柄キーワード選別を適用し、
直接関連→市場共通の順、その中で公開時刻降順→source ID→article IDで整列。URL一致または空白正規化・小文字化
したタイトル＋要約の完全一致を重複除去する。類似記事の意味的統合はしない。
要約は先頭700文字（切り詰めフラグあり）、最大5記事・共通記事JSON 2400 UTF-8 bytes。
入らない記事は理由を保存して除外。モデル別の選別はしない。
将来feedを増やす際もallowlistと選別方針を事前に固定し、別DBで開始する。

全適格snapshot、選別・除外結果、選択した全article/version ID、ニュース集合ID、
共通stateのSHA-256、共通質問、プロンプト版、config、実装ファイルhashを保存する。
4アダプターは同じstateを受け取り、並行して一度ずつ呼ぶ。シリアル順番による差を避ける。
参考価格・テクニカル指標・外部検索を入力に加えないニュースのみの実験である。
ニュースの命令文はデータとして扱う。ツールや検索はモデルに提供しない。
モデルの学習済み知識の影響までは除去できないため、正式評価は今後の前向き収集のみ。

予測はT+60秒以内に開始、T+120秒までの正常回答だけ評価対象。遅い回答も保存。
全入力はAPI呼出し前にDBへ保存し、直後の収集更新で変わらない。
ニュースなし、またはallowlist内の有効feedがstaleなら全モデルを見送る。
同じ記事が隣接時間の24時間窓に残ることは許容し、記事IDから追跡できる。

## API仕様と支給後の接続確認

公式資料を2026-11-12に確認した。ユーザーのアカウントでの利用権限は未確認。

| エンジン | 初期ID / 呼び方 | 確認状況 |
|---|---|---|
| GPT-6 Astra | `gpt-6-astra`, POST `https://api.openai.com/v1/responses` | 公式ID確認。Responses、low effort、最大1024出力、検索なし |
| Claude Fable | `claude-fable-5-2`, POST `https://api.anthropic.com/v1/messages` | 公式ID確認。Messages、low effort、最大1024出力 |
| Jev | `jev-latest`, 既存TypeSafe SDKの`system_one`と`Choice` | 本番SDKの署名・再試行設定を読取確認。既存1時間用predict関数は呼ばない |
| Kairos | `DMIND/Kairos`, POST `https://minara.cloud/v1/systemone` | Minara公式仕様に合わせる。専用Kairosグループキー必須 |

参照: [OpenAIモデル](https://developers.openai.com/api/docs/models/gpt-6-astra)、
[Claudeモデル](https://platform.claude.com/docs/en/models/fable-5-2/overview)、
[Minara System One](https://minara.cloud/docs/system-one)、
[DMind API](https://dmind.ai/docs/kairos/api-reference/systemone)。
Claudeは[現行API版](https://platform.claude.com/docs/en/api/versioning)の
`anthropic-version: 2026-10-21`を明示し、Workspaceに所属するAPIキーを想定する。

Minaraの `general` は任意質問、`news_return` は固定テンプレート・12項目のstate・
7つのreturn_bin・指定horizonを使う。後者の中央binは0をまたぐので、その確率から
「厳密に上昇」の確率を一意に復元できない。今回 `general` の二択を採用し、
`news_return` は設定検証で拒否。将来は独立実験とし、混合集計しない。
DMind側の例は `kairos` だが、Minaraのcatalog IDは `DMIND/Kairos`。
実際のレスポンスmodel、model_version、calibration_versionを保存し、未返却はnull。
providerのcalibration_versionだけでは当実験で校正済みとは扱わない。

4つとも `probability_up` を保存し、0.5以上をUP、それ未満をDOWNとする。
Kairos/Jevのconfidenceは独立の集中度フィールドであり、上昇確率や勝率へ変換しない。
全確率は未校正。分布・有限値・応答完了を検証し、不正応答はエラーとして残す。
最大出力で思考が終わらない場合も再試行せずエラー。出力枠の変更は新しい実験にする。
実装済み出力の形は `phase8a/result.schema.json`。
8preに置かれた `news/phase8a-output.schema.json` は初期設計案として残しており、
実装版の代わりには使用しない。実装版は失敗・遅延と校正区分も明示した平坦な形式。

APIキーは別途ご支給予定。`phase8a.env.example` は空欄テンプレートであり、
実値はサーバーの秘密ファイル/環境へ設定する。会話、設定JSON、Gitには入れない。
このCLIは既存 `.env` を自動で読まない。HTTPヘッダーは保存せず、既知のキーが
応答へ混ざった場合も保存前にマスク。エラーの生メッセージは出力しない。
systemdはEnvironmentFileを読む。手動実行では、信頼できる自分の秘密ファイルから
環境変数をexportしてからCLIを実行する（値をechoしない）。

支給後は各社のモデル一覧/コンソールで利用権限・ID・Kairosグループ・料金を確認し、
config内のaccount_verified/pricing_verifiedと単価を設定する。
OpenAI/Fableの例示単価は公式の標準トークン単価。Jev/Kairosは不明につきnullで停止。
現在の認証情報・モデル一覧・実課金・ゲートウェイの応答互換性は未検証である。

## 課金・失敗・再実行

有料呼び出しには `--live` と `live_enabled=true` の両方が必要。
さらに4エンジン全部のキー、確認済み料金・権限、Jev SDKを事前確認する。
一部のキーが欠けたまま他だけを課金しない。失敗理由を4エンジン分記録する。
最初は全体4呼び出し、合計/UTC日ごと1 USD、1呼び出し0.25 USDの予約枠。
入力bytesに余裕を加えたトークン上限推定と出力枠・確認済み単価が予約内に収まるか確認。
この計算はローカルの保守的な制限で、事業者の課金額の保証ではない。
プロバイダー側にもクォータを設定し、料金変更や複数DB/他アプリの利用を別途管理する。

4回分をSQLiteの排他的トランザクションでまとめて予約してから通信する。
429、5xx、タイムアウトも初版では**自動再試行0回**。Jev SDKの既定2回も無効化。
失敗やプロセス停止後も予約額を解放しない。結果不明の呼び出しを重複課金しないため、
同じ時刻の再実行は何も送信しない。通信途中の停止はreservedとして残る。
再試行が必要なら請求確認後に、別の将来時刻で実施する。
単価に基づく費用推定と生usageは保存するが、請求書による実費確定は次段階。

config・ソースが変わった場合は既存DBで続行を拒否する。別DBにして条件変更を明示。
ただし別DBで予算をリセットすることを運用上の増額手段にしないこと。
全体の課金上限は専用キー側のクォータと併用する。

## 評価とペーパートレード

Coinbase BTC-USD 5分アーカイブを読み取り専用で使用。参照価格はT直前5分足の終値、
4時間後はT+4h直前5分足の終値。完全に一致する足のみ使用し、補間・外部再取得なし。
将来の価格がそろってから、取得時刻とsource・OHLCを検証し、実現値と元の足を一度保存。
終値が同値の場合はbinary事象「上昇ではない」=DOWN、flat件数も表示する。
Phase 6のFLAT扱いとは異なるため、スコアを混ぜない。

Accuracy、Brier、LogLoss（0/1確率のlogのみ1e-15でclip）を算出。
全4モデルの正常・期限内回答がそろう同時刻集合の指標を比較する。
時間ごとの結果は重複窓を含む記述統計。主評価はUTC epoch起点の4時間刻み
（00/04/08/12/16/20 UTC）で非重複にする。欠測でanchorをずらさない。
モデル単独の利用可能データによる指標も分母付きで別表示する。
0.5一定確率の基準、失敗・見送り・遅延・flat・未評価件数も残す。
有意差検定、信頼区間、校正学習、モデル選択、正式な標本数設計は次段階。

ペーパーは非重複時刻だけ、p>=0.6でlong、p<=0.4でshort、その他見送り。
全モデル共通のT+5分の足の始値で仮想エントリー、T+4時間で決済。
予測完了締切のT+2分より後なので、応答前に取引した扱いにならない。
約3時間55分の保有になり、4時間の方向スコアとは別の実行可能性の近似である。
固定1000 USD、往復20bp（手数料・スプレッド・スリッページの合算仮定）。
実際の約定、流動性、空売り借入・資金調達費を再現するものではない。
複利・レバレッジなし。モデル自身の失敗/見送りを含む運用成績として集計する。
重複時間・失敗・未決済を含むそのモデルの全API予約費用を差し引いた保守的PnLも表示。
未決済件数を併記し、請求確定済みの純利益とは呼ばない。

## 定期実行の準備と運用

本番には独立配置する。既存timer変更、有料テスト、定期予測開始は行わない。
`deploy/systemd/btc-phase8a.*` は別ディレクトリ `/opt/btc-phase8a` 用の準備済みテンプレート。
本番の凍結済みcheckoutへ上書きするより、独立配備を推奨する。
既存Python環境を利用し、ニュースDB・価格DBは読み取り専用、新規DBだけ書き込む。

1. テスト済みソースを別ディレクトリへ配備し、所有者・read/write権限を確認する。
2. config exampleを `phase8a.config.json` へコピーして確認済み設定を固定する。
3. `.env.phase8a` をローカルで設定しmode 0600、配備先は0700または0750にする。
4. `check` とoffline予行を別DBで実施する。news healthと入力件数を確認する。
5. futureの整時に1回だけ `predict --live`。最初は4回/合計1 USD上限のまま検証する。
6. 保存した4回答・課金・モデルID・遅延・確率を確認し、4時間後にevaluate/reportする。
7. 継続する場合は予算・観測期間を決めた別実験を準備してからtimerを有効にする。

```sh
python3 -m phase8a.cli check --config phase8a.config.json
python3 -m phase8a.cli predict --live --config phase8a.config.json --db phase8a.db --news-db /opt/btc-news/news.db
python3 -m phase8a.cli evaluate --db phase8a.db --candles-db /opt/btc-predict/candles.db
python3 -m phase8a.cli report --db phase8a.db
```

timerは毎分10秒にtickし、configのinterval_secondsに該当する開始窓だけ予測する。
その他は期限到来済み価格を照合。取り逃した過去時刻を実APIで補完しない。
予測間隔変更はconfigで行う（5分の倍数、horizonの約数）。
8preの収集間隔は別設定 `news.config.json` のinterval_secondsとbtc-news.timerを合わせる。
1時間収集の設定例は `phase8a.news.config.example.json`（interval_seconds=3600）。
対応する収集timerのOnCalendarは `*-*-* *:00:00 UTC`。
現行15分収集は変更していない。整時までにコミット済みの記事のみ予測に入るので、
T直後の収集結果は次の予測へ回る。収集と予測の同時起動で締切を後ろへ動かさない。

停止は新規btc-phase8a.timerだけを止める。予約/結果DBは削除せず、SQLite backup APIで
WAL整合性を保って退避する。既存btc-web/candles/Phase 6/6b/7/newsは再起動不要。
残作業: 暗号資産ニュース源の確保、API支給と各社接続確認、料金確定、少額前向き実測、
校正・正式な研究計画・news_return独立実験・追加銘柄の定期運用・UIは別段階。

## 今回の検証記録

- macOS Python 3.9.6: news + phase8aの63件、成功（Jev SDK実体テスト1件のみ未導入でskip）。
- 本番と同じPython 3.12.3/既存依存環境の独立コピー: 全体240件成功。
  その後の1時間収集設定・APIヘッダー最終調整を含む関連63件も成功、skipなし。
- 全体検証の最初の実行は一時コピーのGit情報不足で44件エラー。
  独立コピーに検証用Git情報を用意して全240件を再実行し、成功した。
  既存UIテストのファイル未close警告は残るが、テスト失敗ではない。
- Jevの実SDKをローカル模擬HTTP transportで実行し、リクエスト形式・回答正規化・
  自動再試行0回を確認。ネットワーク推論は発生していない。
- 本番ニュースDBへの読み取り専用offline確認: 0記事、no_eligible_news、呼出しなし。
- 合成デモで4モデル相当の保存・価格突合・非重複指標・ペーパー損益まで確認。
  合成結果はモデルの性能や収益性の証拠ではない。
- systemd定義のverifyと毎分10秒UTCのcalendarを検証。単位のインストール/起動なし。
- 既存Phase 6/6bの凍結ソース・価格保存コードの差分なし。既存web/news timer/
  candles timer正常、価格収集serviceの直近終了successを確認。
- APIキーの読み出し/追加、有料推論、実取引、本番コード配備、既存設定変更は0件。

`outputs/` にテストログ、ソースhash、offlineデモレポート、検証サマリーを保存。
実装ブランチは `phase8a-news-comparison`。追加指示によりコミット・GitHub push・独立本番配置を行う。
PR作成・main統合は対象外。

## API設定待ちの本番配置

配置先は `/opt/btc-phase8a`。`SOURCE_COMMIT`と`source-manifest.json`で配置物を識別する。
`phase8a.config.json`は`live_enabled=false`。`.env.phase8a`は空欄テンプレート、mode 0600。
定期実行unitはテンプレートのみ配置し、systemdへのインストール・enable・startは行わない。
確認はcheckと実ニュースのoffline予行のみ。本番予測DBはまだ作成しない。
既存 `/opt/btc-predict` と `/opt/btc-news` のコード・設定・タイマーは変更しない。
