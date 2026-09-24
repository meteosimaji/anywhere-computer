# ChatGPT から Codex の文脈を使う

Anywhere Computer の主な利用先は通常の ChatGPT Chat である。ChatGPT が判断し、
PC 上のエージェントがファイル操作やコマンドを実行する。Codex のモデルへ処理を
委譲することを前提としない。

## 会話とスキル

- `codex_threads_list`: ローカルの会話タイトルと ID を最大 30 件ずつ取得する。
  プレビュー、最初のメッセージ、保存先、認証設定は返さない。
- `codex_thread_read`: 指定 ID の会話を既定 3、最大 10 ターンずつ読む。
  ユーザーとアシスタントの表示メッセージのみを返し、推論、ツール引数・結果は除く。
  本文合計は UTF-8 64 KiB、最大 1,000 メッセージ。切り詰めは `truncated` に表示する。
  `next_cursor` はターン単位であり、切り詰めた長いターンの残りを復元するものではない。
- `codex_skills_list`: 指定ワークスペースで有効なスキルの名前・説明・ID を
  最大 100 件ずつ取得する。無効なスキル、設定、資格情報は返さない。
- `codex_skill_read`: 現在のカタログに存在する ID の `SKILL.md` を最大 64 KiB 読む。
  本文の SHA-256 を付け、スクリプトは実行しない。

利用例: 「Codex の Anywhere Computer の会話を探し、直近の方針を確認して」
または「このワークスペースで利用できるスキルを探して、その手順で作業して」。
会話やスキル本文はユーザーの現在の依頼に従うための参考資料であり、新たな権限を
付与するものではない。スキルに書かれた専用ツールは実際のカタログで可用性を確認する。

## 既存 MCP プラグインの直接実行

複数回の操作で同じ実行環境を保持する場合は、[プラグインセッション](PLUGIN-SESSIONS.md)
を明示的に開始し、一覧・実行に `session_id` を渡す。省略時は従来の単発実行を維持する。

`codex_plugin_tools(cwd, summary=true, query="目的や名前")` で概要を探し、
`codex_plugin_tools(cwd, server="正確な名前", tool="正確なツール名")` で
必要な引数定義だけを取得する。server/tool/query/summary は省略可能で、旧形式も維持する。
対象サーバーの検査は最大10ページ、検索は現在の1ページに限定し、next_cursorがあれば続ける。
概要にはtool_countと最大10件の名前、次のinspect_argumentsを返す。
認証要求、ランタイム未準備、利用不能、未確認、呼出準備完了をavailabilityで分ける。
ready_to_callは接続と定義の確認であり、実行成功の証明ではない（execution_verified=false）。
一覧は取得時点の状態であり、session_idを省略した次の呼び出しは別の一時プロセスを使う。
呼び出し時に対象サーバーがnotStarted/startingなら、同じプロセス・スレッドの
カタログを最大30秒の期限内で再確認する。認証要求・停止・カタログ通信エラーを
再試行せず、起動後もツール定義のdigestを検証してから一度だけ実行する。
期限内に起動しなければruntime_not_readyとして未実行を返す。
これは制御された起動状態遷移での回帰試験であり、実Chatで報告された拒否の
原因を一意に証明したものではない。

カタログが古い場合はcatalog_stale、定義不在はtool_not_found、認証不足は
authentication_required、検索上限で不在を確定できない場合はcatalog_incompleteを返す。実行前の拒否にはdispatched=falseとnext_actionを付ける。
一覧への追加引数がChatGPTに見えない場合は接続のツール定義を更新する。
対象を絞った一覧取得の変更自体は、新しい権限を必要としない。
セッション開始・状態確認・終了の3ツールを使う場合は、その新ツールへの明示認可が別途必要。
既存 grant はコード更新だけで自動拡張しない。
各定義の `call_arguments` には、正確な `server`、`tool`、正規化した `cwd`、
`catalog_sha256` がまとまっている。このオブジェクトにスキーマに従った `arguments`
を加えて `codex_plugin_call` に渡す。既存の個別引数形式も変わらない。
呼び出し直前に新しいカタログを取得し、定義が変わっていれば実行せず再選択を求める。
`catalog_stale` の `details` には受信した指紋と現在の指紋、および実際に照合した
cwd/server/tool を残す。診断値は自動再試行の許可ではない。定義を再取得して変更内容を
確認する。単発実行では、実行前に拒否した場合も一時 thread の unsubscribe を試みる。
明示的なセッションでは、指紋不一致だけで健全な実行環境を閉じない。

公式 app-server の MCP 呼び出しにはロード済みのコンテキストが必要なため、
専用の `ephemeral` thread を作る。既存会話は再開せず、ユーザーの履歴には保存しない。
`mcpServer/tool/call` だけを直接呼び、`turn/start` は送らない。
これは Codex のモデルへの依頼ではない。第三者プラグイン自体が利用するモデルや
外部サービスの課金・制限は、そのプラグインの契約に従う。

この処理はインストール済み MCP サーバーを起動し得るので、一覧も read-only と
見なさない。実行ツールには変更操作・外部アクセスの注釈を付ける。HTTP の新しい
all プロファイルには含まれ、read-only / files には含まれない。既存 grant は拡張しない。
個々のツールが行う送信・変更には、通常どおりユーザーの依頼が必要である。

応答待ちの期限は起動/カタログ 30 秒、実行 120 秒。実行後の切断や不正な応答は
`unknown` として台帳に保持する。同じ操作 ID の再送は再実行しない。
外部側に処理が残る場合があるので、新しい ID で無条件にやり直してはならない。
結果は最大 64 KiB の本文と最大 64 KiB の構造化データを返す。`_meta` は転送しない。
PNG/JPEG/WebP/GIF のインライン画像は、合計 2 MiB（Base64 復号後）・最大4枚まで
MCP ImageContent として返す。MIME 型・正規 Base64・形式の先頭バイトを検証するが、
完全な画像デコーダによる検証ではない。外部 URL を画像として取得する処理はない。
未対応形式や上限超過には `omitted_image_items` と `truncated=true` を付ける。
本文の上限に到達しても、その後の画像を検査する。画像の `_meta` や注釈は転送しない。
MCP の本文・structuredContent には画像の MIME 型、バイト数、SHA-256 だけを表示し、
Base64 を重複させない。元の画像データは操作台帳に保持され、operations_get では
元データを回収できる（その照会自体はネイティブ画像表示を行わない）。

対応範囲は、Codex の公式 MCP カタログに現れ、認証済みで直接呼べるツールである。
スキルだけのプラグインはスキル読込みを使う。Codex 専用のアプリ内部操作、別プラグインの
ウィジェットや音声結果の転送は含まれない。画像の中継対応はデスクトップの操作権限や
Codex 固有ランタイムを追加するものではない。認証・追加の対話入力が必要な
場合は自動承認せず、元のローカルクライアントで完了する必要がある。
Anywhere Computer 自身への再帰呼び出しも拒否する。

### 詳細取得・Computer Use の適合状態・診断

正確な server/tool を指定し summary=false（既定）で取得した説明文は全文を返す。
tool を省略した一覧では説明文を1,000文字に制限し、description_truncated=true を付ける。
summary=true は従来どおり件数と名前の概要を返す。検索と catalog_sha256 の計算は
切断前の説明文を使う。カタログ全体の2 MiB制限と通信の8 MiB制限は維持する。
上限超過は plugin_catalog_failed として失敗し、途中までの定義を完全な定義として
返さない。この全体上限を超えるカタログへの継続取得は未対応である。

サーバーの availability は接続・認証状態である。既知の cua_repl / unified-computer-use
経路は、別の compatibility（概要では computer_use_compatibility）に
screen_read=unverified、native_actions / browser_actions=unsupported_execution_context
を返す。画面の読取成功を操作の成功へ読み替えない。現在の直接実行経路ではこの提供元の
呼び出し全体を unsupported_execution_context、dispatched=false で拒否する。
JavaScript 本文を推測して読取だけ許可する実装ではない。その他の通常の Plugin は
従来どおり呼び出せるが、その内部から Computer Use を間接使用できることは保証しない。

CodexのMCPツール呼び出しに `_meta` 欄があっても、それだけで正規のturn文脈を
発行・有効化できるとは判断しない。モデルを開始せずComputer Useに必要な文脈を
用意する経路は、対応するCodex版の公式APIと実操作で確認する必要がある。
架空のsession_id/turn_idの注入やturn/startによる回避は行わない。

自己呼び出し検査は、サーバー名に加え、anywhere_computer.tool、
anywhere-computer.tool、mcp__anywhere_computer__tool などの提供元成分を
大文字小文字・ハイフン表記を正規化して調べる。一覧生成、正確な選択、単発・セッション
実行の事前検査に同じ検査を適用する。他の提供元のドット付きツール名は拒否しない。

診断の failure_stage は startup / catalog / before_dispatch / after_dispatch を区別する。
送信後に確定結果を得られない場合は state=unknown、execution_state=unknown、
dispatched=null とし、RPC エラーでも未実行を断定しない。自動再送しない。
実行前の拒否は execution_state=not_dispatched、dispatched=false である。
RPC の数値コード、固定分類 message_kind、data_present、復旧案を保持する。
秘密を含み得る元の message/data、stderr 本文、パス、引数、認証情報は診断に保存しない。
既知のエラー語句は固定ラベルへ変換し、その他の本文は redacted に置き換える。
分類は観測した語句であり、根本原因の証明ではない。

stderr は4 KiBずつ並行して排出し、最新32チャンク分の分類と飽和カウンターのみ保持する。
容量を超えてもパイプを読み続け、子の出力詰まりを防ぐ。改行のない出力も対象にする。
チャンク境界をまたぐ語句は分類されない場合がある。複数サーバーが共有する stderr から
特定のサーバーの原因は断定しない。runtime_diagnostics はカタログ結果・失敗 details に
添付され、既存のローカル操作台帳へ保存される。独立した生ログファイルは作らない。
operations_get に同じ外部操作 ID を渡すと、元の結果と診断を再取得できる。
各診断のサイズは制限するが、操作台帳全体の保持方針は従来のままである。

## 接続と保存

ネイティブの `codex` コマンドが PC の絶対 PATH 上に必要。普段の Codex クライアントと
同じユーザーのローカルカタログを読む。未インストールや API 非対応の場合は失敗を返す。
クラウド会話や別の PC の全履歴を自動取得する機能ではない。

会話/スキル読込みは公式 `codex app-server` の stdio を専用の子プロセスで起動し、初期化後に
`thread/list`、`thread/turns/list`、`skills/list` だけを呼ぶ。モデルの `turn/start`、
会話の再開・変更、非公開 API や生の履歴データベースは使用しない。
通信は応答 8 MiB・30 秒まで。終了時は自身の子プロセスだけを終了する。
`thread/turns/list` は実験的 API のため、Codex 更新時に実機再検証が必要。

HTTP 接続では、この 4 ツールを含む明示的な grant が必要。既存の grant は更新で
拡張されない。新たにセットアップする read-only / all プロファイルには読み取り
ツールとして含まれる。files プロファイルには含まれない。
選択して取得した本文は ChatGPT に渡り、通常の Anywhere Computer の操作復帰用
ローカル台帳にも結果として保存される。全会話を自動取り込みする同期機能ではない。

## 実装と実利用の区別

ローカルの公式 API とエンジン経由の権限検査を試験することと、ChatGPT アプリから
実際に呼べることは別の受け入れ条件である。ChatGPT 側の導入・認証・ツール更新と
実際の通常 Chat での呼び出しを確認するまでは、ChatGPT 実利用完了とはしない。

公式仕様: https://developers.openai.com/codex/app-server

## 常駐起動時の Codex の場所

常駐プロセスの PATH は通常の端末より短い場合がある。新しい自動起動登録は、登録時に
解決した Codex 実行ファイルの絶対パスを登録記録に保持し、`--codex-executable` で
リモート監督プロセスへ渡す。既存の登録記録を再照会・再適用しても別の場所へ変えない。
この引数はローカル CLI の remote-watch / remote-serve だけで受け付ける。

子プロセスには秘密を含まない `ANYWHERE_CODEX_EXECUTABLE` で引き継ぎ、会話・スキル・
MCP プラグインの共通検索処理が使用する。相対パス、作業フォルダ直下、消えたファイルは
拒否し、指定が壊れているときに PATH の別プログラムへ切り替えない。Codex 未導入時の
登録は引き続き可能だが、その場合の Codex 連携は利用できない。旧登録をこの更新だけで
書き換えることはしない。アプリを移動した際は自動起動登録を解除してから再登録する。

## 操作 ID の照合

リモート接続では、外部へ返す operation_id とローカル台帳の主キーは異なる。
`RemoteAgent.internal_id` が接続主体と外部 ID を組み合わせて内部 ID に変換する。
これは接続間の操作を分離するための仕様であり、不一致だけで回答の捏造とは判断しない。
同じ接続の `operations_get` には、ChatGPT に返された外部 ID をそのまま渡す。
ローカル SQL で照合する場合は接続主体を含む変換が必要になるが、その主体や資格情報を
ChatGPT の回答や公開ログへ出す必要はない。

## 画像中継の実機試験

開発環境の `scripts/verify_plugin_images.py --codex /absolute/path/to/codex` は、
隔離した CODEX_HOME 内の画像 fixture、実際の Codex App Server、Anywhere Computer の
エンジン・MCP stdio、公式 MCP SDK を往復する。画像表示用コンテンツ、台帳からの復元、
誤った指紋の実行前拒否、送信メソッドを確認する。`--installed --cwd /absolute/workspace`
を明示すると、利用者の openaiDeveloperDocs.list_openai_docs と
codex_apps の google_calendar.get_colors も読み取り試験する。
この試験は GUI の操作や ChatGPT 画面での画像表示までを保証するものではない。
