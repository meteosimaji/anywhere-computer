# HTTP/Codex連携の仕組み：2026-09-22 追加試験

本文は [仕組みと実装計画](../../SUBCHAT-HTTP-MECHANISMS-2026-09-22.ja.md)。これは通常Chatのライブ送信成功や、第三者アプリ全体の品質保証ではない。

## 保存した証拠

| ファイル | 内容 |
|---|---|
| [source-manifest.json](source-manifest.json) | 5プロジェクト、取得した31ファイルの固定commitとSHA-256。取得と全文監査を区別 |
| [upstream-characterization.json](upstream-characterization.json) | 実上流関数のAST抽出による12件の観測。実行した定義名・元の行範囲・hashを含む |
| [reference-test-result.json](reference-test-result.json) | 新規SSE framingとlocalhost HTTPの21試験、71分割位置、HTTP POST2回 |
| [upstream-test.sanitized.log](upstream-test.sanitized.log) | 上流特性試験のログ。ローカルパスを含むasyncio timing診断だけ省略 |
| [reference-test.sanitized.log](reference-test.sanitized.log) | 参照実装試験の同様のログ |
| [evidence-manifest.json](evidence-manifest.json) | 同梱コード・結果のhash。ログは原本hashと公開版hashの両方を記録 |
| [probes](probes) | 今回作成したframing参照実装と試験。外部認証・保護処理のコードは含まない |
| [run_checks.py](run_checks.py) | 新しい作業ディレクトリを作り、試験を実行するrunner |

公開前のrunner経由の追加実行とpackage/runtime試験は、ツール境界で実行前に拒否され、別経路で再実行していない。保存した12件・21件は各試験スクリプトの直接実行結果である。runner自体のend-to-end実行は未確認。公開コードと直接実行した原本の同一性はハッシュで照合する。

12件の上流試験が成功というのは、指定した関数の振る舞いを予想どおり観測したという意味である。切り詰めやキャンセル時の枠消失も成功した観測に含まれる。問題が修正されたという意味ではない。21件の参照試験は、独立に書いた受信コードの合格条件を検査している。

対ChatGPT生成POST、認証情報の読み取り、ブラウザー起動、上流アプリのインストールは行っていない。上流Pythonモジュール全体はimportせず、hashの一致したファイルから明示したAST定義だけを取り出して実行した。HTTPを扱う上流関数はメモリー上のfakeで検証し、その試験中のsocket接続は禁止した。参照transportの試験では実際の127.0.0.1へのHTTPを使い、外部socket接続とsubprocess起動を禁止した。

## 受信参照実装を再実行する

Python 3.12と `httpx==0.28.1` が必要。既存のAnywhere開発環境を使う場合も、実行するPythonを明示する。以下の出力ディレクトリは存在しない新しいパスにする。

```sh
python docs/research/2026-09-22-http-mechanisms/run_checks.py \
  --output /absolute/new/research-output
```

`run_checks.py` はコードを新しいディレクトリへコピーし、そこに結果を保存する。既存結果を上書きしない。成功時はexit 0、`test_sse_reference.py` の21件が実行される。HTTP受信が成功しても通常Chatが作成されたことにはならない。

`SSEDecoder` はUTF-8/BOM、改行3形式、field/value、空行でのevent区切り、上限を扱うframing層である。JSON差分の適用、ChatGPT認証、生成POST、履歴のfinal確定、EventSourceの自動再接続は実装していない。不正UTF-8の拒否とbudgetは明示的なlocal policyであり、ブラウザーの全挙動を実装したものではない。

## 上流の特性試験を再実行する

公開した [source-manifest.json](source-manifest.json) の固定commitから、次の3ファイルを通常のGitHubの読み取りで取得する。第三者ソースそのものはこの証拠フォルダーに同梱していない。パッケージのインストールや認証設定は不要。

| sourceディレクトリからの相対パス | 必要なcommit |
|---|---|
| `suphotP--chatgpt-api/chatgpt_api/providers/chatgpt/transport.py` | `f998a6d83f324cb3187396dd7efced0c40f29601` |
| `suphotP--chatgpt-api/chatgpt_api/api/openai_compat.py` | 同上 |
| `robotlearning123--gpt2agent/gpt2agent/tools/conversations.py` | `38a78fe8affb7d23955f01b45a98924b3e7714ab` |

```sh
python docs/research/2026-09-22-http-mechanisms/run_checks.py \
  --sources /absolute/pinned-public-sources \
  --output /absolute/another-new/research-output
```

runnerは上記3ファイルだけをコピーし、各定義を実行する前にハッシュを検証する。ソースが違えば拒否する。12件の特性試験と21件の参照試験の結果が別々に出る。外部ソースの自動取得・認証探索・provider通信はrunnerの機能に含めない。

## 解釈上の制限

SSEの空白なし形式や複数data行は一般仕様の入力を合成している。ChatGPTが現在それらを実際に返すことを再現した試験ではない。本文切り詰めも、上流の表示用ツールを完全回答の回収器として転用する際の問題であり、表示用仕様そのものを一律に欠陥と呼んでいない。

Semaphoreのキャンセル問題は実際のasyncio/threadingの組合せで再現したが、試験中のaccount・要求・枠は合成物である。最後に取得済み枠を解放し、停止した待機threadを残していない。新しいasyncio.Semaphoreの例は同一event loop内の対処であり、クロスプロセスrate limitの実装ではない。

基準版は `61d38e091ac4d5d1a179c6cee759255d9ceb9d8b`。製品コードと配布wheelの変更は行わず、研究フォルダーは標準pytestのtestpathsに加えない。既知不具合を隠すためのskip/xfail追加や、既存テストの弱体化はない。
