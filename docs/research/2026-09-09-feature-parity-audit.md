# Desktop Commander 機能充足監査

監査日: 2026-09-09。実装照合点: Anywhere Computer `a65083afee69759cc08931a4b9432379c54d0060` を基準に、
端末入力/プロンプト待機の追加をDC15/EX07へ統合（検証は [TERMINAL](../TERMINAL.md)）。

**結論: 全機能は満たしていない。40 ツールという数は互換性の証拠にならない。**
標準ツールの入力・処理・出力を照合した。terminal から外部ソフトや自作スクリプトを呼べること、
ホストの別プラグインが提供する機能は実装済みに数えない。本表は追加済み実装を各行に統合したもの。検証済み実装と未確認の実環境条件を区別する。

## 比較対象と判定規則

比較元は初回調査のスナップショットであり、この更新で外部資料を再取得したものではない。

- 2026-09-09 に確認した [Remote 公式 README](https://github.com/desktop-commander/remote-desktop-commander#available-tools) の 30 機能カテゴリを確認。
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
|DC03|write_file|部分|テキスト・バイナリ保存に加え、DOCX本文、複数シートXLSX（文字列/数値/真偽値/明示数式）の生成・全体置換。hash/backupあり。既存Officeの書式保持編集・数式再計算なし。|
|DC04|write_pdf|未対応|Markdown から PDF 作成、ページ挿入・削除、別名出力の専用処理なし。|
|DC05|create_directory|対応|directories_create: parents=True / exist_ok=True。|
|DC06|list_directory|対応|directories_list: 再帰・隠し項目・上限・truncated。深さ最大8、最大2000項目、directory symlink は辿らない。|
|DC07|move_file|部分|同一ファイルシステムのファイル・フォルダ・symlinkをOSの排他的renameで移動。別ファイルシステムへの移動なし。|
|DC08|get_file_info|部分|ファイル種別、mode bits、各時刻、リンク自身と参照先の情報。非対応の作成時刻はnull。形式固有メタデータなし。|
|DC09|edit_block|部分|files_edit は exact match 件数と hash 検査。Office 編集、不一致候補提示なし。|
|DC10|start_search|部分|名前/UTF-8/限定OOXML内容、literal/隔離子プロセスregex、glob、大小文字、単語境界、文脈行、時間・容量上限。文書検索はセル/段落の位置とhash付き。PDF検索なし。同期OS呼出しの強制中断は保証しない。|
|DC11|get_more_search_results|対応|search_results の cursor/limit と状態・上限理由。|
|DC12|stop_search|対応|search_stop と協調的 worker 中断。|
|DC13|list_searches|対応|search_list の ID/状態一覧。agent instance 内の検索のみ。|
|DC14|start_process|部分|terminal_start: shell/cwd・非同期開始・長時間実行。timeout/対話可能判定/verbose timing なし。|
|DC15|interact_with_process|部分|terminal_inputで入力と最大30秒の応答待機、literal wait_for_promptを提供。容量/欠落/終了/timeoutを区別。PTY・プロンプト自動判定なし。|
|DC16|read_process_output|部分|絶対byte cursor、負cursorによる末尾取得、欠落量、終了コード、最大30秒のwait_ms。verbose timing/プロンプト認識なし。|
|DC17|force_terminate|対応|terminal_stop: 管理中セッションと process group/tree の終了処理。任意 PID 用ではない。|
|DC18|list_sessions|対応|terminal_list: 管理中セッション ID/PID/状態/終了コード/cursor。|
|DC19|list_processes|対応|processes_list: OSプロセスのPID、起動時刻、名前、状態、資源情報。引数・環境変数は非表示。|
|DC20|kill_process|対応|processes_stop: PIDと起動時刻を照合し終了。自身と祖先を保護。OS権限による拒否は返却。|
|DC21|get_config|部分|settings_get: 永続する既定shellと読書き行数上限。allowedDirectories/blockedCommandsなし。|
|DC22|set_config_value|部分|settings_update: 型付き永続設定。既定shellは全クライアント共有の実行関連権限。許可ディレクトリ/禁止コマンド設定なし。|
|DC23|get_usage_stats|対応|usage_stats: 永続台帳のtool/state別件数集計。操作ID単位で集計。|
|DC24|get_recent_tool_calls|部分|operations_recent: ツール名完全一致と開始時刻下限で絞込み。引数・結果プレビューは返さない。operations_getは既知ID照会。|
|DC25|get_prompts|部分|skill/README の導入説明はあるが、ID 指定のプロンプトライブラリなし。|
|DC26|feedback|未対応|独自フィードバックフォームを開く専用ツールなし。第三者フォームへの送信は代替要件ではない。|
|DC27|list_devices|部分|CLIの永続SSH/HTTP登録と、ローカルコネクターのdevices_list/tools/callで会話から明示IDによる転送。公開HTTPへの再転送権限は追加しない。共有dashboard・pairingなし。|
|DC28|who_am_i|未対応|認証実装はあるが、現在の認証ユーザーを返す専用公開ツールなし。|
|DC29|ping device|部分|computer_status と CLI device-status の認証済み照会あり。MCP protocol ping は接続先端末の稼働保証ではない。deviceId 指定 pong/時刻ツールなし。|
|DC30|shutdown agent|部分|CLI stop はローカル agent 停止。端末 ID 指定で応答後に遠隔 agent を止める公開ツールなし。|

集計: **対応10 / 部分17 / 未対応3**。これは30項目の分類数であり、完成率ではない。
「対応」でも API 名・入力・返却形式は独自で、drop-in replacement ではない。

## EX01–EX17: ツール名以外の全項目

|ID|領域|判定|不足・確認範囲|
|---|---|---|---|
|EX01|テキスト|部分|CRLF、末尾、hash、限定置換、backup/restore。編集不一致の候補提示なし。|
|EX02|画像/URL|部分|byte/base64 転送はある。画像 handler/MIME/render、URL fetch はない。|
|EX03|Excel|部分|シート/保存値/数式/A1範囲の読取、型付き値と明示数式による複数シート生成。書式保持セル編集・追記・数式再計算なし。|
|EX04|DOCX|部分|本文と表内段落の文字抽出、プレーンテキストからDOCX生成。表構造・outline・画像・header/footer・書式保持編集・Markdown生成なし。|
|EX05|PDF|未対応|形式としての読取/作成/ページ操作なし。|
|EX06|検索|部分|DC10に記載。DOCX/XLSX/PPTXの限定内容検索を実装。PDF非対応、長いフィールドは切詰めを明示。文書モードの文脈行は拒否。|
|EX07|端末|部分|実プロセスの入出力試験あり。PIPE 方式、PTY なし。各 REPL/SSH/DB CLI の個別互換、指定文字列のプロンプト待機は実装・実プロセス試験済み。自動認識は未対応。agent 再起動を越えたセッション復元なし。|
|EX08|プレビュー UI|部分|MCP Apps resource と画像/抽出文書の表示。隔離ブラウザーで PNG 表示と HTML 非実行を検証。実 ChatGPT/Codex、文書 UI は未検証。Markdown/HTML 整形なし。|
|EX09|編集 UI|部分|隔離ブラウザーから実 Engine に接続し、CRLF 保存、競合拒否、部分読込時の編集禁止、移動時の破棄確認を検証。実 ChatGPT/Codex は未検証。選択文脈・保存後 undo なし。|
|EX10|フォルダ UI|部分|一階層の一覧と上限を増やす追加表示。隔離ブラウザーで一覧とファイル選択を検証。追加表示と実 ChatGPT/Codex は未検証。ツリー展開・OS file manager 起動なし。|
|EX11|設定 UI|部分|共有設定3項目の画面。隔離ブラウザーで読込上限を変更し DB 保存・再読込・幅420px表示を検証。実 ChatGPT/Codex は未検証。初回接続・OS 権限・自動起動画面なし。|
|EX12|実行設定|部分|既定shell・読書き行数上限の永続設定APIあり。blockedCommands/allowedDirectoriesなし。HTTP tool scopeはOS sandboxではない。|
|EX13|履歴|部分|永続operation状態/要約、tool/since絞込み。引数ログは抑制。保持期間/ローテーションは未完。|
|EX14|導入運用|部分|共通 Python/CLI、Codex package、3 OS 自動起動 adapter。無害な CI worker の登録/停止証拠あり。実ログイン/再起動後の公開接続、更新/rollback、sleep 復帰、初心者向け導入の完成検証なし。|
|EX15|Remote 接続|部分|HTTPS/SSH、OAuth PKCE、native vault、refresh/revoke、CLI端末選択と会話中の明示ID routing。照合コードpairing/dashboard、二台の実機実証は未達。|
|EX16|Remote 安定性|部分|重複抑制/unknown、子プロセス監督、公開edge経由のconnector crash復帰証拠。任意の公開メタデータ監視を実装（既定無効・認証稼働の証明ではない）。ネットワーク変更/sleepの実証は未完。|
|EX17|サポート|部分|README/skill/診断文書あり。独自プロンプト集、feedback/usage 入口、日本語/英語全導線整備は未完。人的優先サポートは運営要件。|

画面操作/Accessibility/ブラウザ DOM/OCR/音声等は追加目標であり、今回確認できた Desktop Commander MCP の30公開ツールには含まれない。
相手製品にない機能を互換不足に混ぜない。単体 Desktop Commander App の独自チャットやモデル課金も別製品の範囲。

## 実装・検証の根拠

- 登録/schema/dispatch: `src/anywhere_computer/engine.py`, `models.py`, `mcp_server.py`。40名をregistryから公開し重複登録を拒否。ローカルstdioには専用ルーター3名を追加し、名前衝突・多段転送を拒否。MCP image/resources/prompts の提供なし。
- 形式/ファイル/検索/端末: `files.py`, `documents.py`, `search.py`, `sessions.py`。対応する `tests/test_engine.py`, `test_documents.py`, `test_search.py` と照合。
- 端末選択/履歴: `devices.py`, `cli.py`, `state.py`, `remote_bridge.py` と対応テスト。CLI機能とMCP公開ツールを区別。
- 上記コミットの実装検証: **470 passed / 5 skipped**。Ruff、mypy通常/Windows対象（50 source files）、plugin構造検証、同梱wheel全50 Pythonファイルとソース/checksum一致が成功。これはOS全条件・全機能の完成証明ではない。
- セキュリティ修正の追加再確認: 関連pytest **54 passed / 3 skipped**。内部Python起動の隔離と旧自動起動登録の移行を確認。[監査と修正](2026-09-09-code-execution-audit.md)。
- 実装対応: Office生成は `document_writer.py`、プロセス管理は `processes.py`、実行設定は `state.py` / `engine.py`、排他的移動は `move_native.py`、公開監視は `remote_health.py`。各機能の専用テストも存在。
- 公開接続の既存証拠: [constant internet receipt](2026-09-09-constant-internet-verification.json)。同一macOSから公開edge経由の隔離エンジン。17 MiB/更新/失効/接続子クラッシュから6.5958545秒で復帰。二台の物理端末、本番常駐、実ブラウザ描画を証明しない。
- OS 起動の既存証拠: [native startup receipt](2026-09-09-native-startup-verification.json)。三OSのCIで無害なworkerの実登録/起動/解除。実ユーザーのログイン後のremote接続とは別。

## 未達を解消する順序

1. URL/画像/PDF、Office書式保持編集、端末のPTY/プロンプト自動認識、独自ヘルプと設定の残不足を埋める。実装済みのフォルダ移動・プロセス管理・統計・検索拡張を未実装扱いに戻さない。形式別の fixture と失敗時保全を受け入れ条件にする。
2. 会話からの端末選択は実装済み（[DEVICE-ROUTING](../DEVICE-ROUTING.md)）。identity・agent停止、pairingとdashboard、プレビュー/編集/設定UIを作る。
3. 実装済み公開メタデータ監視の限界を踏まえ、認証付き経路とネットワーク断/sleep/再起動/ログイン/更新の障害試験を行い、macOS/Windows/Linux と二台の実機で証拠を残す。
4. 完成した候補で Codex/ChatGPT 実利用と配布を検証し、公式申請を行う。現在は private alpha であり、全機能代替として公開しない。

依存最小化の方針は維持する。ただし、未実装の形式処理や OS 差分が依存なしで既に解決したとは扱わない。
