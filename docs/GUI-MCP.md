# GUI操作を既存のMCPへ接続する

## alpha8の型付き操作

alpha8にはPeekaboo向けの薄いアダプターを追加した。先に既存の実行ファイルを
`mcp_session_open`で明示的に選ぶ。別のMCPが同じ名前のツールを持つだけでは互換性を保証しない。

- `gui_observe(session_id, app)`で対象アプリを前面化・観測し、observation_idを受け取る。
- `gui_click(session_id, observation_id, element_id)`で観測中の要素をクリックする。
- `gui_type(session_id, observation_id, text, press_return)`は対象アプリを前面化して入力する。
- `gui_key(session_id, observation_id, keys)`は対象アプリを前面化してキーを押す。

キー名は大文字・小文字を区別しない。`escape`/`esc`、`return`/`enter`、
`tab`、`delete`、`space`、`cmd`、`shift`、`alt`、`option`、`ctrl`、`fn`、
英数字1文字、`arrow_up/down/left/right`、`f1`〜`f12`に対応する。
例: `["ESCAPE"]`、`["cmd", "a"]`。入力schemaにも対応名を表示する。
appは選択したMCPが返す実際の名前を使う。日本語macOSのCalculatorは「計算機」と
返ることがある。観測自体は起動せず、必要ならappツールのschemaを読み、起動結果の名前を使う。
`computer_status.capabilities.gui_mcp_adapter`はこのアダプターの登録と必要条件を示す。
利用者が選択する外部MCPの動作をstatusだけで検証済みとは扱わない。

観測は接続所有者・MCPセッションに結び付き、60秒または次の観測・操作で失効する。
入力・キー操作はOSの現在のフォーカスを使うため、外部からのフォーカス変更は防げない。
この制約と送信・削除等の副作用をツール説明に明示している。アダプターは独自の承認画面を
追加せず、ホストの判定を回避することも保証しない。再試行で入力を繰り返さない。

開発版では同じエンジンの型付きGUI操作を直列化する。観測または操作の進行中に
別の型付きGUI要求が届くと、待ち行列へ入力を積まず、実行前にbusyを返す。
前面化とキー送信の間にもこの制御を保持する。検証済みの入力を開始する直前に、
他セッションを含む保存済み観測を失効させ、次の入力には新しい観測を要求する。
キャンセルや例外では実行枠を解放する。この制御は同じGUIMCPインスタンス内に限られ、
手元の操作、汎用mcp_call、別エンジン、外部アプリのフォーカス変更は防がない。
実機で観測された入力不一致の原因をこの競合と断定したものではない。

観測結果にPeekabooのversion 1 coordinate_contextがあれば、座標系・原点・矩形・画像寸法・
snapshot参照を検証して保持する。それ以外の_metaは転送しない。現地の3.0.0-beta3は
このメタデータを返さず、coordinate_status=unavailableとなる。座標クリックは提供しない。
最新版の全機能互換を宣言せず、実際のschemaと応答で対応を確認する。

公開HTTPに追加する場合、既存入口を停止して`http-add-tools`へgui_observe/gui_click/
gui_type/gui_keyの4 scopeを指定し、再起動後にChatGPTのPlugin定義を更新する。
`verify_gui_http.py --typed`はこれらの型付き経路で電卓の連続操作と結果回収を検証する。

直接MCPの`mcp_tools`はsummary=trueで短い一覧、queryで説明全文の検索、nameで正確な
ツール名のschema取得を提供する。フィルターは現在のページに適用されるため、空の結果でも
nextCursorがあれば続ける。全文schemaが必要なときはsummaryをfalseにする。

開発版alpha7は、`mcp_session_open` → `mcp_tools` → `mcp_call` で、利用者が
インストールしたstdio MCPへ直接接続できます。Codexのモデル実行や登録は不要です。
MCPのツール定義を読んでから、接続元のAIが操作を選びます。

macOSの実機では、既存のPeekaboo 3.0.0-beta3と既存の画面収録・アクセシビリティ権限を
使い、認証付きHTTPから電卓の観測、入力、再観測、6段階の連続計算、結果回収を確認しました。
他OS、他バージョン、任意のアプリに対する実操作を保証する試験ではありません。
PeekabooやCodexのアプリ本体をAnywhere Computerに同梱・再配布していません。

## 接続例

実際のインストール先に合わせ、絶対パスを指定します。

```json
{
  "command": ["/opt/homebrew/bin/peekaboo", "mcp", "serve", "--no-remote"],
  "cwd": "/absolute/workspace"
}
```

1. `computer_status` を確認する。
2. 上記を `mcp_session_open` に渡し、返った `session_id` を保持する。
3. `mcp_tools` で実際のツール名と引数を取得する。
4. `mcp_call` に `session_id`、`name`、`arguments` を渡す。
5. 同じセッションで観測と操作を続け、最後に `mcp_session_close` する。

この版のPeekabooでは `see` の `app_target` でアプリを指定できます。入力直後の観測は
古い表示を返すことがあります。操作を二重実行せず、期待する表示になるまで短い上限時間で
観測だけ繰り返してください。応答消失時も入力を再送せず、まず操作IDで結果を回収します。
`agent` や `analyze` は別のモデルを使う機能です。ローカルGUI操作だけが目的なら使いません。

`capabilities.gui=false` はAnywhere Computer内蔵のGUI実装がないことを表します。
外部MCPによるGUI操作の可否は、選択したMCPの接続、OS権限、実際の観測結果で判断します。
Codex専用の `cua_repl`／`computer-use` が再利用可能になったという意味ではありません。

## 更新後に直接MCPのツールが見えない場合

エンジンの更新とHTTPで許可されたツール集合は別です。既存のHTTP入口を停止したうえで、
管理者が明示的に次を実行し、同じ入口を再起動します。`--state-dir` はHTTP用ディレクトリです。

```sh
anywhere http-add-tools --state-dir '/absolute/http-state' \
  --scope mcp_session_open --scope mcp_session_status --scope mcp_session_close \
  --scope mcp_tools --scope mcp_call
```

既存の接続URL、認証情報、操作台帳は置き換えません。旧ツール集合の全件を許可している
有効な認可だけに追加し、制限付き・失効済み・期限切れの認可は変更しません。
これは自動更新ではなく、追加機能を公開するローカル管理コマンドです。
保存途中の障害で設定と認可が一致しない場合、サーバーは起動を拒否します。
同じコマンドを再実行すると、その追加内容に一致する中断状態だけを復旧します。
ChatGPTでは「設定 → プラグイン → Anywhere Computer → 更新する」でツール定義を
更新し、上記5ツールが表示されることを確認してください。実機では新規チャットを開くだけでは
旧49ツールのキャッシュが残り、この更新操作後に直接MCPの5ツールが表示されました。
更新前から開いていた会話は旧定義のままでした。更新後に新規チャットを開くと5ツールの
取得、Peekabooのセッション開始、schema取得、結果回収、終了まで成功しました。
接続先の認証情報を作り直す操作ではありません。他のクライアントでも、カタログキャッシュが
残る場合はクライアント側の更新・再接続が必要です。

alpha7に対する2026-09-13のGPT-5.6 SolによるChatGPT実試験では、「すべてのアクションを許可」が
選択されていても、最初の `see` を含む `mcp_call` がOpenAI側の安全性チェックで拒否され、
操作IDは発行されませんでした。セッションは維持され、終了とcleanupは成功しました。
この時点では、HTTP機械試験の成功だけでChatGPTからのGUI受け入れ合格とは扱いませんでした。
Anywhere Computerに独自の毎回承認を追加したものではなく、この設定だけで全呼び出しの
実行を保証することもできません。

alpha8の型付きツールでは、更新後のGPT-5.6 Sol（中程度）で観測・Escape・入力・42の再観測・
セッション終了が実成功しました。キー表記の手戻りも修正して再試験済みです。
詳細は[更新検証記録](UPDATE-VERIFICATION-ALPHA8-2026-09-13.md)を参照してください。
この一試験の成功が、ホスト側の判定を将来含めて保証するものではありません。

## 再現可能な実機試験

### 2026-09-14: 電卓以外の受け入れ結果

alpha9の認証付きHTTP接続を使ったGPT-5.6のChat試験では、MacのChromeで
`example.com`から`example.org`へ同一MCPセッション内で移動し、URLと
`Example Domain`の表示を確認した。操作台帳の6呼び出しとセッション終了は
成功し、別経路の画面観測とも一致した。任意のWebサイトや長時間連続操作の保証ではない。

追加のAnywhere Computer直接呼び出し試験では、TextEditの新規文書への入力・
全選択・置き換えを実行した。最終的に日本語、絵文字、数値42を含む3行が
独立したアクセシビリティ観測で全文一致した。試験用MCPセッションの終了も確認した。
この追加試験の判断主体はCodexであり、新規ChatGPT試験とは区別する。

この試行では次の未解決事項も観測した。

- `type`は成功応答を返したが、入力した先頭行が本文に残っていなかった。
  入力方法・IME・フォーカスのどれが原因かは未確定であり、成功応答を全文入力の証拠にしない。
- `paste`のアプリ／ウィンドウ指定ではフォーカス取得に失敗した。
  実際の本文と選択範囲を再確認してから、現在フォーカスへの貼り付けを行うと全文が一致した。
  この結果を、自動的な対象指定省略や失敗後の無条件再送の根拠にはしない。
- Windowsは接続と状態取得に成功したが、この試験でWindowsアプリのGUI実操作は
  実証していない。登録されたPeekaboo向けschemaをWindows用GUIバックエンドと誤認しない。

連続操作の受け入れでは、操作結果内の`is_error`、実際の入力先、本文／画面の変化を
それぞれ確認する。操作台帳の`completed`だけでアプリ側の成功を判定しない。

### 同日の追加試験: 成功応答と表示の不一致

稼働中のalpha9へCodexから公開ツールを直接呼んだ追加試験では、
TextEditの新規書類とChromeの新規タブを観測できた。一方、次の操作は
期待結果を確認できず、合格としない。新規ChatGPTからの試験ではない。

| 操作ID | 操作 | 再観測した結果 |
| --- | --- | --- |
| `0869898b351f4eeda0de57dbaea0da90` | TextEditに3行を入力 | 後ろの2行のみを取得。先頭行の反映は確認できない |
| `acff8d63ff4a4846ac39c2c27f8356c3` | 全選択後に「編集後: 42 🚀」を入力 | 本文は元の2行のまま |
| `2d4f8557adb647f0943ec0a97208304b` | Chromeの新規タブにURLを入力してReturn | 指定ページへの移動を確認できない |
| `41d44e75ca374c57b84bdb49200914ef` | ChromeでCmd+W | 新規タブ表示が残り、タブ数も期待と不一致 |

上記はいずれもproviderの成功応答を受けたが、アプリ側の反映を証明しない。
同時操作、フォーカス、入力配送、観測の遅延を切り分けていないため、
入力消失や特定の原因を断定しない。観測画像が細い黒い画像になる問題も再現した。
独立したAppleScriptによる本文取得は応答待ちを中断し、照合未完了とした。
後続の`peekaboo bridge status --json`では選択先はlocalだったが、
これは各入力時点の配送先を遡って証明するものではない。

試験用セッション`e75fe69a79574d9ba9173444b9f5a687`は終了し、
`cleanup_confirmed=true`を確認した。試験書類とタブは状態不一致のため
追加の閉鎖操作を繰り返さず残した。GUI全体の受け入れは未完了であり、
電卓の成功や過去の個別試行の成功でこの不一致を除外しない。

この試験は電卓を前面に出し、現在の式を変更します。既存権限を確認してから実行してください。
認証付きローカルHTTP、直接MCPセッション、実際の電卓を使用します。HTTP認証情報は
試験専用でメモリ内に生成し、本番接続は変更しません。

```sh
uv run python scripts/verify_gui_http.py \
  --executable /opt/homebrew/bin/peekaboo --receipt /tmp/anywhere-gui-acceptance.json
```

通常の機械試験は `tests/test_http_capability_acceptance.py` です。alpha8のHTTP公開対象58ツールを
実際のHTTP要求で網羅し、ファイルとプロセスは使い捨ての実物、Codex app-serverは合成データを
返す子プロセスを使います。外部Plugin全件の実操作やChatGPT UIの試験とは区別します。

参考: [Peekaboo MCP documentation](https://peekaboo.sh/MCP.html)

開発版の観測は、指定アプリを前面化した後に `see(app_target="frontmost")` を使う。
Peekaboo 3.0.0-beta3のアプリ指定経路が固定ウィンドウ番号0を選び、
Chromeで1920×30の帯を返した実機事例に対応する。前面経路では961×979の
Chrome画面を取得できた。返却されたApplication行が指定名と一意に完全一致しない場合は
`action_ready=false`、`observed_application_mismatch`とし、操作用IDを発行しない。
アプリ名は実際の表示名を指定する。PID・bundle ID・翻訳名の別名解決は行わない。
この変更は同一アプリ内の特定ウィンドウへの固定や、入力結果の成功を保証しない。

### 提供元の診断情報と電卓以外の試験

MCP結果の `provider_diagnostics` は、提供元が返した既知の状態値と真偽値だけを
保持する。自由記述、認証情報、対象の詳細を含み得るメタデータは転送しない。
これは提供元の申告であり、Anywhere Computerによる効果確認や実行許可ではない。
`is_error` の値は変更しない。診断がない場合も未送信とは解釈しない。
`retry_safe` などの値で自動再送は行わず、実行結果が不明なら先に再観測する。

2026-09-14の隔離評価では、Peekaboo 4.3.4を既存インストールと別の場所から起動し、
直接MCP経路でテキストエディットの試験用ウィンドウを明示して観測・入力・再観測した。
日本語・英語・絵文字を含む3行の置換は、AX本文と画像の両方に反映された。
一方、入力結果は `is_error=true`、`dispatch_state=dispatched`、
`state=dispatched_unverified`、`retry_safe=false` だった。再入力は行わず、
後続の読み取りで効果を確認した。MCPセッションの終了も確認した。

この評価は通常ChatGPTの受け入れや既存の型付きGUIアダプターの4.3.4対応を
証明しない。4.3.4は入力に明示的なsnapshotを要求し、3.0.0-beta3とは引数が異なる。
また、評価したbrowserツールの背景専用経路では新規接続・ページ遷移が提供されない。
既存バイナリの置換だけでブラウザー操作まで対応済みとは扱わない。
