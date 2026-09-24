# GUI操作の接続と確認

GUI操作は実行中のツール一覧、選択したMCPのschema、OS権限を確認してから始めます。
開発ソースにはmacOS向けNative Accessibilityと、明示的に開いたMCP sessionを
使うPeekaboo／Cuaアダプターがあります。ソースに実装があっても、古いインストール済み
エンジンに同じツールがあるとは限りません。GUI操作にsubchatやChatGPTブラウザーの
sessionは不要です。

操作の成功応答は画面や文書の変更を証明しません。対象ウィンドウを再観測し、
意図した値・表示・保存状態を独立に確認します。応答が失われた場合は操作IDで結果を
回収し、状態を確認する前に入力を再送しません。

## macOS Native Accessibility

`gui_native_windows(app)` は正確なbundle IDを指定し、所有者に紐づくsessionと
ウィンドウ一覧を返します。その `session_id` と `window_id` を
`gui_native_observe(session_id, app, window_id)` に渡します。観測した要素に対して、
次を使用できます。

- `gui_native_set_value(session_id, app, window_id, observation_id, element_ref, value)`:
  書き込み可能なAXValueを置換し、同じ要素の値を読み戻します。ファイル保存の確認ではありません。
- `gui_native_press(session_id, app, window_id, observation_id, element_ref)`:
  `pressable` と表示された要素へAXPressを1回実行します。

操作後は再観測します。最後に `gui_native_close(session_id)` でsessionを終了します。
macOSのAccessibility権限とmanifest検証済みのnative helperが必要です。
この経路は座標クリック、スクリーンショット、キー送信、Windows UI Automationを
提供しません。アプリの前面化を代替手段として実行しませんが、アプリ側の動作で
フォーカスやウィンドウが変わることはあります。
`capabilities.gui=false` だけで別登録の `gui_native_*` の可否を判断せず、
稼働中のツール一覧、helper、権限、対象アプリを個別に確認してください。

## 外部MCP sessionの開始

既存のMCP実行ファイルを `mcp_session_open` で選び、返された `session_id` を保持します。
`mcp_tools` で実際のツール名とinput schemaを取得し、使い終えたら
`mcp_session_close` を呼びます。同名のツールがあるだけではPeekaboo／Cuaの
契約との互換性は確定しません。外部MCPやCodexのアプリ本体をAnywhere Computerに
同梱・再配布しません。

Peekabooの接続例です。実際のインストール先に合わせて絶対パスを指定します。

```json
{
  "command": ["/opt/homebrew/bin/peekaboo", "mcp", "serve", "--no-remote"],
  "cwd": "/absolute/workspace"
}
```

`mcp_tools` は `summary=true` で短い一覧、`query` で説明の検索、`name` で
正確なツール名のschema取得を提供します。結果が空でも `nextCursor` があれば
続けて確認します。schema全文が必要なら `summary=false` を指定します。
直接の `mcp_call` では、選択したMCPのschemaに従い操作します。
Peekabooの `agent` や `analyze` は別のモデルを使う機能です。

### Peekabooの型付き操作

選択したPeekabooの `window` ツールで `action=list` と対象 `app` を指定し、
実際の `window_id` を取得します。`gui_observe(session_id, app, window_id)` は
対象を観測し、互換性のある `see`、`click`、`type`、`press` schemaと
ウィンドウ・snapshotの結び付きを確認した場合だけ入力用の観測IDを発行します。
返された `observation_id` と観測内の要素を `gui_click`、`gui_type`、
`gui_key` に渡します。対象ウィンドウを省略した入力や、旧版の前面フォーカス入力は
使用できません。

`gui_type` は観測した入力要素を `element_id` で指定でき、`clear=true` で
既存文字列を置換できます。Returnが必要なら新しい観測を取得してから
`gui_key(keys=["return"])` を別に呼びます。操作は観測を消費し、
`postcondition_verified=false` を返すため、対象ウィンドウを再観測します。
window/snapshot契約の最終的な強制は選択したMCPにも依存します。

試験用のTextEdit文書を用意した場合、隔離HTTPテストは次の形式です。
既存の本文が指定した試験文字列に一致しない場合、書き込み前に停止します。

```sh
python scripts/verify_gui_http.py --typed --executable /absolute/path/to/peekaboo \
  --window-id ACTUAL_ID --app ACTUAL_APP_NAME \
  --expected-text 'Anywhere GUI 日本語 ✅' --receipt /tmp/gui-acceptance.json
```

### Cuaの型付き操作

開発ソースのCua経路は、明示的に選択したCua MCP sessionに対する
window-scoped投影です。`gui_observe` に `provider="cua"`、実際の `pid`、
`window_id`、`app` を指定します。アダプターは `get_window_state` と
`click`、`type_text`、`set_value`、`press_key` のschemaを調べ、
対象PID・ウィンドウ・アプリ名、完全な要素ツリー、snapshotと要素tokenを
確認した場合だけ操作可能な観測を返します。スクリーンショットは要求しません。
`query` は返す要素とその祖先を絞り込みます。

`gui_click`、`gui_type`、`gui_key` はその観測の `observation_id` と
`element_id` を使用します。Cuaでの `gui_type(clear=true)` はAXValueの
置換 (`set_value`)、`clear=false` は対象への文字入力 (`type_text`) です。
文字入力には観測したテキスト要素が必要です。Returnは別の `gui_key` 操作と
再観測で行います。入力は `scope=window` と `delivery_mode=background` に
固定されますが、選択したCua MCPとOSが契約どおり動くことは別途確認が必要です。
このソース実装を、インストール済みのCua接続や実機GUI受け入れの証拠としては
扱いません。Codex専用の `cua_repl`／`computer-use` を再利用する機能でもありません。

## HTTP接続で直接MCPツールが見えない場合

エンジンの更新とHTTP入口の許可ツール集合は別です。既存のHTTP入口を停止し、
管理者が同じHTTP設定保存先に対して必要なscopeを追加して再起動します。

```sh
anywhere http-add-tools --state-dir '/absolute/http-state' \
  --scope mcp_session_open --scope mcp_session_status --scope mcp_session_close \
  --scope mcp_tools --scope mcp_call
```

型付きGUI操作をHTTPに公開する場合は、そのツールscopeも明示的に追加します。
クライアント側にカタログのキャッシュがある場合はツール定義を更新・再接続し、
実際のツール一覧とschemaを確認してください。旧会話のカタログは更新されない
ことがあります。この手順は接続URLや認証情報を作り直しません。
