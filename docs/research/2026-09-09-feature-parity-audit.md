# Desktop Commander 機能充足監査

監査日: 2026-09-09。対象: Anywhere Computer `0365c5eca75d98a09802428dcc1bd9b9cf6297ef` と現在の作業ツリー。

**結論: 全機能は満たしていない。34 ツールという数は互換性の証拠にならない。**
標準ツールの入力・処理・出力を照合した。terminal から外部ソフトや自作スクリプトを呼べること、
ホストの別プラグインが提供する機能は実装済みに数えない。製品コードは変更していない。
未コミットの公開経路監視は開発途中なので充足判定から除外する。

## 比較対象と判定規則

- 現行 [Remote 公式 README](https://github.com/desktop-commander/remote-desktop-commander#available-tools) の 30 機能カテゴリを確認。
- ローカル [公式 manifest](https://raw.githubusercontent.com/wonderwhy-er/DesktopCommanderMCP/main/manifest.template.json) に 26 ツールが存在。package.json は 0.2.48。
- Remote [server.json](https://raw.githubusercontent.com/desktop-commander/remote-desktop-commander/main/server.json) は 1.0.22。GitHub release の版と同一とは扱わない。
- 詳細比較の固定点は [既存台帳](2026-09-08-findings.md): ローカル commit `56deabc3fe3c586f91728c56da5715712ff34eb6`。
- Remote の完全な現行 tools/list は再取得していない。DC27–30 の関数名・deviceId は前回セッションの宣言に基づく。今回の公式資料では機能カテゴリを再確認した。
- 「対応」は主要機能を独自 API で提供する意味で、同名・同 schema・全 OS の境界条件まで互換という意味ではない。
- 「部分」は機能の一部または CLI の代替が存在。「未対応」は要求する専用機能がない。

## DC01–DC30: 全ツール機能

|ID|比較機能|判定|現在の実装と不足|
|---|---|---|---|
|DC01|read_file|部分|files_read は UTF-8・負 offset・hash。documents_read は OOXML の限定抽出。URL 取得、画像 MIME/MCP image 出力、PDF 抽出なし。バイナリ転送は画像表示の代替ではない。|
|DC02|read_multiple_files|部分|files_read_many は最大20個のテキストと個別エラー。形式別の複数読み取りはない。|
|DC03|write_file|部分|テキスト作成・置換・追記とバイナリ保存あり。Excel/DOCX の構造を扱う書込みなし。既存更新は hash 必須。|
|DC04|write_pdf|未対応|Markdown から PDF 作成、ページ挿入・削除、別名出力の専用処理なし。|
|DC05|create_directory|対応|directories_create: parents=True / exist_ok=True。|
|DC06|list_directory|対応|directories_list: 再帰・隠し項目・上限・truncated。深さ最大8、最大2000項目、directory symlink は辿らない。|
|DC07|move_file|部分|files_move は同一ファイルシステムの通常ファイルを排他的 hard-link/unlink。フォルダ・symlink・別ファイルシステム移動なし。|
|DC08|get_file_info|部分|path/size/modified/directory/symlink。権限、他の時刻、形式固有情報が不足。|
|DC09|edit_block|部分|files_edit は exact match 件数と hash 検査。Office 編集、不一致候補提示なし。|
|DC10|start_search|部分|名前/UTF-8内容、literal、glob、大小文字、隠し項目、単語境界、件数/深さ/ファイル上限。regex・文脈行・時間上限・Office 内容検索なし。|
|DC11|get_more_search_results|対応|search_results の cursor/limit と状態・上限理由。|
|DC12|stop_search|対応|search_stop と協調的 worker 中断。|
|DC13|list_searches|対応|search_list の ID/状態一覧。agent instance 内の検索のみ。|
|DC14|start_process|部分|terminal_start: shell/cwd・非同期開始・長時間実行。timeout/対話可能判定/verbose timing なし。|
|DC15|interact_with_process|部分|terminal_input は入力送信のみ、出力取得は別ツール。wait_for_prompt と応答待機なし。|
|DC16|read_process_output|部分|terminal_output は正の byte cursor・欠落 byte 数・終了コード。負 offset による末尾取得、待機 timeout/timing なし。|
|DC17|force_terminate|対応|terminal_stop: 管理中セッションと process group/tree の終了処理。任意 PID 用ではない。|
|DC18|list_sessions|対応|terminal_list: 管理中セッション ID/PID/状態/終了コード/cursor。|
|DC19|list_processes|未対応|OS 全体のプロセス一覧・資源情報のツールなし。psutil の依存だけでは実装にならない。|
|DC20|kill_process|未対応|任意のシステム PID を指定する専用ツールなし。|
|DC21|get_config|部分|接続用 CLI 設定・診断はあるが、実行エンジン設定取得の MCP ツールなし。|
|DC22|set_config_value|部分|接続設定 CLI はあるが、実行エンジンの型付き設定更新なし。|
|DC23|get_usage_stats|未対応|稼働時間・active count はあるが、成功/失敗等の利用集計なし。|
|DC24|get_recent_tool_calls|部分|operations_recent は要約のみ。引数を表示しない設計。toolName/since 絞込み・結果プレビューなし。operations_get は既知 ID の結果照会。|
|DC25|get_prompts|部分|skill/README の導入説明はあるが、ID 指定のプロンプトライブラリなし。|
|DC26|feedback|未対応|独自フィードバックフォームを開く専用ツールなし。第三者フォームへの送信は代替要件ではない。|
|DC27|list_devices|部分|CLI の永続 SSH/HTTP 登録・一覧・名前・選択あり。MCP 会話からの端末一覧/各呼出しの deviceId routing、アカウント共有 dashboard なし。|
|DC28|who_am_i|未対応|認証実装はあるが、現在の認証ユーザーを返す専用公開ツールなし。|
|DC29|ping device|部分|computer_status と CLI device-status の認証済み照会あり。MCP protocol ping は接続先端末の稼働保証ではない。deviceId 指定 pong/時刻ツールなし。|
|DC30|shutdown agent|部分|CLI stop はローカル agent 停止。端末 ID 指定で応答後に遠隔 agent を止める公開ツールなし。|

集計: **対応7 / 部分17 / 未対応6**。これは30項目の分類数であり、完成率ではない。
「対応」でも API 名・入力・返却形式は独自で、drop-in replacement ではない。

## EX01–EX17: ツール名以外の全項目

|ID|領域|判定|不足・確認範囲|
|---|---|---|---|
|EX01|テキスト|部分|CRLF、末尾、hash、限定置換、backup/restore。編集不一致の候補提示なし。|
|EX02|画像/URL|部分|byte/base64 転送はある。画像 handler/MIME/render、URL fetch はない。|
|EX03|Excel|部分|シート・セル・保存値/数式文字列の抽出。A1 範囲・行単位契約、作成/追記/セル編集なし。|
|EX04|DOCX|部分|本文段落と表内段落の文字抽出。表構造・見出し outline・画像参照・header/footer・XML 編集・Markdown 作成なし。|
|EX05|PDF|未対応|形式としての読取/作成/ページ操作なし。|
|EX06|検索|部分|DC10 に記載。Office を単純 UTF-8 検索しても文書検索対応にはならない。|
|EX07|端末|部分|実プロセスの入出力試験あり。PIPE 方式、PTY なし。各 REPL/SSH/DB CLI の個別互換、プロンプト待機は未検証/未対応。agent 再起動を越えたセッション復元なし。|
|EX08|プレビュー UI|未対応|MCP は tools-only。Markdown/画像/HTML/Office の UI resource なし。|
|EX09|編集 UI|未対応|編集・undo・選択文脈・部分読取マージの UI なし。ファイル backup は UI undo と別。|
|EX10|フォルダ UI|未対応|ツリー・遅延ロード・追加読込・OS file manager で開く UI なし。|
|EX11|設定 UI|未対応|接続承認画面は設定編集 UI の代替ではない。|
|EX12|実行設定|未対応|blockedCommands/allowedDirectories/defaultShell 等の永続設定 API なし。呼出し単位 shell 指定や HTTP tool scope とは別。|
|EX13|履歴|部分|永続 operation 状態/要約あり。引数ログは抑制する設計。高度な検索/保持期間/ローテーションは未完。|
|EX14|導入運用|部分|共通 Python/CLI、Codex package、3 OS 自動起動 adapter。無害な CI worker の登録/停止証拠あり。実ログイン/再起動後の公開接続、更新/rollback、sleep 復帰、初心者向け導入の完成検証なし。|
|EX15|Remote 接続|部分|HTTPS/SSH、OAuth PKCE、native vault、refresh/revoke、CLI 端末選択。照合コード pairing/dashboard/会話中 routing、二台の実機実証は未達。|
|EX16|Remote 安定性|部分|重複抑制/unknown、子プロセス監督、公開 edge 経由の connector crash 復帰証拠あり。公開経路のみの断線・sleep/ネットワーク変更からの回復、heartbeat 完成検証なし。開発中 remote_health は除外。|
|EX17|サポート|部分|README/skill/診断文書あり。独自プロンプト集、feedback/usage 入口、日本語/英語全導線整備は未完。人的優先サポートは運営要件。|

画面操作/Accessibility/ブラウザ DOM/OCR/音声等は追加目標であり、今回確認できた Desktop Commander MCP の30公開ツールには含まれない。
相手製品にない機能を互換不足に混ぜない。単体 Desktop Commander App の独自チャットやモデル課金も別製品の範囲。

## 実装・検証の根拠

- 登録/schema/dispatch: `src/anywhere_computer/engine.py`, `models.py`, `mcp_server.py`。34名を registry から公開し重複登録を拒否。MCP image/resources/prompts の提供なし。
- 形式/ファイル/検索/端末: `files.py`, `documents.py`, `search.py`, `sessions.py`。対応する `tests/test_engine.py`, `test_documents.py`, `test_search.py` と照合。
- 端末選択/履歴: `devices.py`, `cli.py`, `state.py`, `remote_bridge.py` と対応テスト。CLI機能とMCP公開ツールを区別。
- 今回実行: engine/documents/search/devices/mcp_protocol/remote_transport/remote_lifecycle/binary_files/downloads/uploads の **80 passed (6.80s)**。registry schema と duplicate guard のテストを含む。
- 今回の mypy: 45 source files 成功。未コミット remote_health を含む Ruff は UP047 が1件残る。監査のため既存の開発途中コードを変更していない。
- 全体 pytest: **407 passed / 5 skipped / 1 failed (38.89s)**。失敗は `test_packaged_runtime_matches_current_source_and_checksums` で、開発途中の `remote_health.py` が既存同梱 wheel にないため。未完成機能を同梱し直して検査を通すことはしていない。Windows 対象 mypy も45ファイル成功、`git diff --check` 成功。現作業ツリーはリリース可能とは判定しない。
- 公開接続の既存証拠: [constant internet receipt](2026-09-09-constant-internet-verification.json)。同一macOSから公開edge経由の隔離エンジン。17 MiB/更新/失効/接続子クラッシュから6.5958545秒で復帰。二台の物理端末、本番常駐、実ブラウザ描画を証明しない。
- OS 起動の既存証拠: [native startup receipt](2026-09-09-native-startup-verification.json)。三OSのCIで無害なworkerの実登録/起動/解除。実ユーザーのログイン後のremote接続とは別。

## 未達を解消する順序

1. 互換台帳に沿って URL/画像/PDF・Office の入出力、フォルダ移動、metadata、検索、端末、プロセス管理、設定/履歴の穴を埋める。形式別の fixture と失敗時保全を受け入れ条件にする。
2. 会話からの端末管理/選択・identity・agent停止、pairing と dashboard、プレビュー/編集/設定 UI を作る。
3. 公開経路の生存確認、ネットワーク断/sleep/再起動/ログイン/更新の障害試験を行い、macOS/Windows/Linux と二台の実機で証拠を残す。
4. 完成した候補で Codex/ChatGPT 実利用と配布を検証し、公式申請を行う。現在は private alpha であり、全機能代替として公開しない。

依存最小化の方針は維持する。ただし、未実装の形式処理や OS 差分が依存なしで既に解決したとは扱わない。

## 監査後の更新

- DC24: operations_recent にツール名完全一致と開始時刻下限の絞込みを追加。
  引数/結果プレビューは返さない方針を維持し、部分判定を維持。
- DC16: terminal_output に末尾基準の負 byte cursor を追加。返却は絶対 next_cursor で
  継続可能。保持範囲外の欠落量も試験。wait_ms による出力待機も追加。
  verbose timing やプロンプト認識は未対応で部分判定を維持。
- DC10/EX06: 本文検索に前後各最大10行の文脈を追加。各行2000文字、ページ容量による
  分割と cursor 継続を試験。協調的な検索時間上限と取得済み結果の保持も追加。
  同期OS呼出しの強制終了、正規表現、Office 内容検索は未対応で、部分判定を維持。
- DC08: `files_info` にファイル種別、mode bits、アクセス/状態変更/作成時刻、リンク自身と
  参照先の区別を追加。作成時刻非対応は null とする。形式固有情報はまだないため判定は部分のまま。
  契約は [FILE-INFO](../FILE-INFO.md)、試験は `tests/test_file_info.py`。
- EX16: 任意の公開メタデータ監視を追加。既定無効、宛先照合、世代を越えた結果の破棄と
  保存失敗後の継続を試験。実ネットワーク障害試験の完了を意味しない。
  契約は [PUBLIC-HEALTH](../PUBLIC-HEALTH.md)。
