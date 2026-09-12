# GUI操作を既存のMCPへ接続する

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

2026-09-13のGPT-5.6 SolによるChatGPT実試験では、「すべてのアクションを許可」が
選択されていても、最初の `see` を含む `mcp_call` がOpenAI側の安全性チェックで拒否され、
操作IDは発行されませんでした。セッションは維持され、終了とcleanupは成功しました。
したがって、下記のHTTP実機試験の成功をChatGPTからのGUI受け入れ合格とは扱いません。
Anywhere Computerに独自の毎回承認を追加したものではなく、この設定だけで全呼び出しの
実行を保証することもできません。

## 再現可能な実機試験

この試験は電卓を前面に出し、現在の式を変更します。既存権限を確認してから実行してください。
認証付きローカルHTTP、直接MCPセッション、実際の電卓を使用します。HTTP認証情報は
試験専用でメモリ内に生成し、本番接続は変更しません。

```sh
uv run python scripts/verify_gui_http.py \
  --executable /opt/homebrew/bin/peekaboo --receipt /tmp/anywhere-gui-acceptance.json
```

通常の機械試験は `tests/test_http_capability_acceptance.py` です。HTTP公開対象54ツールを
実際のHTTP要求で網羅し、ファイルとプロセスは使い捨ての実物、Codex app-serverは合成データを
返す子プロセスを使います。外部Plugin全件の実操作やChatGPT UIの試験とは区別します。

参考: [Peekaboo MCP documentation](https://peekaboo.sh/MCP.html)
