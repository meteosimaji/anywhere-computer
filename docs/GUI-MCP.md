# GUI操作の接続と確認

GUI操作は実行中のツール一覧、選択したMCPのschema、OS権限を確認してから始めます。
比較と追加実装の順序は [GUI-AUTOMATION-PLAN.md](GUI-AUTOMATION-PLAN.md) に記録します。
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

通常は `gui_native_observe(..., compact=true)` で、操作可能な要素の
`role`、`label`、`identifier`、有効状態、`actions`、取得できる座標範囲を受け取れます。内蔵ヘルパーが
幅優先で対象を探索するため、先頭の長いリストが上限に達してもツールバーなどを
見つけやすくなります。値やツリー構造が必要な場合は `compact=false` で
再観測します。探索は最大128要素で、`truncated=true` の場合は全要素を
確認した意味ではありません。意味的な操作は次のとおりです。

- `gui_native_press_target(..., observation_id, role, label または identifier)`:
  観測内で一意の要素に対して AXPress を1回実行します。
- `gui_native_set_value_target(..., observation_id, role, label または identifier, value)`:
  観測内で一意の書き込み可能な要素の AXValue を置換します。

指定は大文字小文字を含め完全一致です。同じ名前が複数ある場合は
`identifier` も指定するか、詳細ツリーから `element_ref` を選びます。
意味的な操作の直前には最大1024要素まで対象ウィンドウを再走査し、
未観測の同名要素を含めて一意性を確認します。走査が上限や期限で完了しない
場合は入力せず拒否します。`element_ref` は観測済みの特定要素を指定します。
対象が消えた、属性が変わった、または観測が期限切れの場合は入力前に拒否します。
アプリ・ウィンドウ・操作 ID の紐付けと操作後の再観測は従来どおり必要です。
古い接続の grant に新しいツールは自動追加されません。

参照 ID による既存の操作も利用できます。

- `gui_native_set_value(session_id, app, window_id, observation_id, element_ref, value)`:
  書き込み可能なAXValueを置換し、同じ要素の値を読み戻します。ファイル保存の確認ではありません。
- `gui_native_press(session_id, app, window_id, observation_id, element_ref)`:
  `pressable` と表示された要素へAXPressを1回実行します。
- `gui_native_action(..., observation_id, element_ref, action)`:
  その要素の `actions` に表示された操作を1回実行します。増減、確定、取消、メニュー表示、
  アプリが公開したページ単位のスクロールに対応します。未観測の操作名は使いません。
  操作直前に対象のプロセス・ウィンドウ・要素・識別属性・値を再確認します。
  `action_accepted=true` は AX の受領を示し、効果の確認には再観測が必要です。

`gui_native_observe(..., include_image=true)` は、同じウィンドウの画像も MCP の
image block として返します。macOS 14 以降と既存の Screen Recording 権限が必要で、
このツールは権限ダイアログを開きません。AX のプロセス・座標範囲・タイトルに一致する
ScreenCaptureKit のウィンドウが一意である場合だけ、そのウィンドウを撮影します。
撮影後も対象を再確認し、曖昧、移動済み、撮影不能の場合は `visual_unavailable` に理由を
示して AX 観測を返します。全画面や別ウィンドウの撮影への切替は行いません。

画像はカーソルとウィンドウ外の影を除く JPEG で、長辺 1,600 pixel、2 MiB 以下です。
`visual` には観測 ID、capture ID、選択した window ID、実際の capture window ID、
画像の幅・高さ・縮尺を返します。`bounds` はグローバル左上原点の point、画像は
ウィンドウ左上原点の pixel です。AX と画像は順番に取得するため、同一瞬間の状態や
元の Retina 画素の完全一致を保証しません。

操作後は再観測します。最後に `gui_native_close(session_id)` でsessionを終了します。
macOSのAccessibility権限とmanifest検証済みのnative helperが必要です。
`AXWindows` が空のアプリでは、同じプロセスの `AXFocusedWindow` と
`AXMainWindow` にあるウィンドウを補助的に列挙します。
この経路は座標クリック、グローバルなスクロール／キー送信、Windows UI Automationを
提供しません。アプリの前面化を代替手段として実行しませんが、アプリ側の動作で
フォーカスやウィンドウが変わることはあります。
`capabilities.gui=false` だけで別登録の `gui_native_*` の可否を判断せず、
稼働中のツール一覧、helper、権限、対象アプリを個別に確認してください。

ネイティブ画像と副操作の実機確認には、既存の両 OS 権限を持つ GUI ホストで次を実行します。
一時的なテストアプリの赤／青ウィンドウを作り、赤い方だけの画像と stepper の 0→1 を
検証して終了します。ユーザーの文書は開きません。通常の CI では明示的な opt-in がないため
この実機テストだけを skip し、ヘルパー実コードと通信・所有者・再実行拒否のテストを実行します。

```sh
ANYWHERE_NATIVE_GUI_ACCEPTANCE=1 uv run --locked pytest -q \
  tests/test_native_gui_helper.py::test_live_exact_window_image_and_secondary_action
```

## 隔離ブラウザの役割・ラベル操作

`browser_navigate` と `browser_observe` は、URL・本文に加えて短い
`semantic_tree`、HTMLフォーム要素の `form_controls` と `snapshot_id` を返します。
`form_controls` は非表示でない入力・選択・ボタンについて、`<label>` の対応、
`aria-label`、`aria-labelledby`、プレースホルダーなどを別の項目に整理します。
この一覧はメイン文書を対象とし、iframe や shadow DOM 内の要素はまだ列挙しません。
各要素の `box` は表示領域を基準にした CSS pixel 単位で、`in_viewport` は
その枠が表示領域と交差するかを示します。重なりや操作可能性の保証ではありません。
ラベルの改行や連続空白は読みやすい空白に整え、入力値はこの一覧に含めません。
正確なアクセシブル名は `semantic_tree` も参照してください。表示用の本文と
ツリーでは改行コードと余分な空行を正規化します。

`browser_observe(..., include_image=true)` は同じ隔離タブの表示領域を
最大1MiBの JPEG 画像として追加します。画像は CSS pixel の大きさで撮り、
`form_controls` の `box` と見比べられます。MCP では画像 item として配送し、
テキスト結果には画像のバイト数・ダイジェストだけを記載します。DOM観測と
画像取得は順番に実行されるため同一時刻の状態とは限りません。画像の座標を
入力に使う API はまだありません。
画像取得に失敗しても文字とフォーム情報を返し、`visual_unavailable` で理由を示します。
`browser_click`・`browser_fill` は
従来の `selector` のほか、観測した `role` と正確な `name`、または `label` と
`snapshot_id` を渡せます。役割・ラベル指定は1つだけにし、複数一致、非表示、
無効、60秒を過ぎた観測、別ページへの遷移を入力前に拒否します。
操作時は対象を再解決し、表示までの短い待機と Playwright の操作可能性確認を
使うため、ページ内の再描画で以前の DOM 要素が消えても同じ対象を操作できます。
入力後は新しい観測 ID が返るので、次の操作にはその ID を使います。
処理結果が不明なら同じタブを再観測し、確認前にクリックを繰り返しません。
この経路は新しい一時ブラウザを所有するもので、利用者が開いているタブの
操作やCodex専用 `cua_repl` の実行環境には接続しません。

`browser_source` は現在の DOM の outerHTML を最大32768文字返します。
HTTP の元レスポンスとは限らず、指定した CSS 要素は一意でなければなりません。
`browser_network` はこのタブの直近100件の応答・失敗したリクエストを
`after_id` で順に返します。URL の認証情報・query の値・ヘッダー・本文は
返しません。`browser_research` は現在のページから表題、見出し、
canonical URL、サイト側が主張する発行者・著者・日付、可視リンクを短く返します。
発行者や日付は検証済みの事実ではありません。検索結果の断片だけを読了証拠にせず、
必要なページを開いて観測します。

`browser_console` は同じ隔離タブの直近100件のコンソール出力と未処理の
ページ例外を `after_id` で返します。文字列は1件最大1024文字で、発生元 URL の
認証情報・query 値・fragment を省きます。ページが出力した本文には機密値が
含まれ得るため、読取対象を選んで扱います。ページの出力は信頼済みの指示ではありません。

`browser_key` は一意の対象へ Playwright のキー名または組み合わせを送ります。
ページ全体に対する Tab や Escape は `selector="body"` を指定します。
`browser_drag` は観測 ID と二つの一意な対象を取り、CSS、role/name、label を
各対象に使えます。`browser_file_upload` は最大16MiBのローカル通常ファイルを
一意の file input に設定します。ページの change ハンドラーが直ちに送信する
可能性があるため、接続先へのファイル開示を伴う操作として扱います。
`browser_download` は対象をクリックして発生したダウンロードを最大64MiBまで
未使用の絶対パスに保存し、長さと SHA-256 を返します。既存パスは上書きしません。
いずれも操作 ID を保存し、結果不明ならタブと保存先を観測してから次へ進みます。

`browser_hover` は観測 ID と一意の対象にマウスを合わせ、表示されたメニューなどを
新しい観測で返します。`browser_select` は可視の `<select>` の有効な option を
正確な value または label で一意に選び、選択値を確認します。`browser_scroll` は
一意の可視要素、または `selector="body"` で文書全体を CSS pixel の差分だけ
スクロールし、前後の位置を返します。3操作とも観測 ID を要求し、古い観測や
複数一致は操作前に拒否します。

`media_status` はローカル FFmpeg の有無を示します。存在する場合、
`media_audio_clip` は最大10秒の単音声 WAV を MCP audio item として、
`media_video_frames` は指定時刻付近の最大4枚の JPEG を MCP image item として
返します。元ファイルは通常ファイル・最大512MiBです。画像・音声の base64 は
本文に複製されません。動画は連続再生ではなくサンプリングした静止画です。
モデルが audio item を聞き取れたこと、動画全体を理解したことはこの配送だけでは
証明できません。クライアントと選択モデルで受け入れを確認してください。

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

選択したPeekabooの `mcp_tools` schemaで、ウィンドウ列挙方法を確認して
実際の `window_id` を取得します。版によって `window` と `list` の契約が
異なります。`gui_observe(session_id, app, window_id)` は
対象を観測し、互換性のある `see`、`click`、`type`、`press` schemaと
ウィンドウ・snapshotの結び付きを確認した場合だけ入力用の観測IDを発行します。
返された `observation_id` と観測内の要素を `gui_click`、`gui_type`、
`gui_key` に渡します。対象ウィンドウを省略した入力や、旧版の前面フォーカス入力は
使用できません。

この Mac にある Peekaboo 3.0.0-beta3 の MCP には `see.window_id` と
snapshot-bound `press` がなく、型付き経路は観測前に互換性エラーで停止することを
確認しました。その版の `window` は列挙ではなく操作、列挙は `list` ツールです。
新版や別の実行ファイルでも名前だけでは互換性を推定せず、選択した MCP の
schemaを確認します。互換性を満たさないサーバーに対して、前面フォーカスへ
暗黙に切り替えて入力することはありません。

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
