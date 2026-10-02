# MCPクライアントから利用する

Anywhere Computerの基本機能は標準MCPを入口にする。Codexのモデル実行は必要ない。
各AIクライアント自身の利用料金・利用枠は、その提供元の規則に従う。

## ローカルの共通設定

利用するインストール先から `mcp-config` を実行すると、そのPythonと状態保存先の
絶対パスを含む設定が出る。出力だけではエージェント起動・資格情報作成・クライアント登録を
行わない。JSONには `mcpServers`、Codex用TOMLには `mcp_servers` が含まれる。
日本語などは形式に合ったUnicodeエスケープで出力され、クライアント側では元のパスになる。
Windowsでファイルに出力する場合も、コンソールの文字コードに依存しない。

ソース版はリポジトリで依存を準備してから出力する。

```sh
uv sync --locked --python 3.12
uv run --locked anywhere mcp-config
uv run --locked anywhere mcp-config --format toml
```

Python同梱版は展開先で `./anywhere mcp-config`、Windowsでは
`anywhere.cmd mcp-config` を使う。既存の状態保存先を使う場合は、出力コマンドに
`--state-dir` とそのパスを指定する。対話式 `setup` のローカル選択も、起動後にJSONを表示する。

クライアントの既存設定に `anywhere-computer` の項目を追加する。同名の登録があれば
現在の実行先・保存先を確認して更新し、他のサーバー設定をファイルごと上書きしない。
デスクトップ起動時のPATHや作業フォルダーに依存せず、通常のファイル・端末操作に
Codexのインストールは不要。OS資格情報ストアは必要で、Linuxでは対応するストアを
ログインセッションで解除しておく。[導入ガイド](PORTABLE.md)も参照。

出力はそのインストール先を固定するため、フォルダーを移動したり、別のPythonへ移行したり
した場合は同じ状態保存先を指定して再出力し、クライアントを再接続する。設定には資格情報や
トークンを入れないが、ローカルのパスは含まれるため、公開するときは伏せる。

追加のMCPサーバーをAnywhere Computerから直接呼ぶ開発版機能を利用する場合は、
`uv sync --locked --python 3.12 --extra mcp` で追加依存を準備する。
Codex会話・登録済みPlugin・
Skillsの取得は任意の連携であり、Codexがない場合はその呼び出しが失敗する。

## Codex

`mcp-config --format toml` の出力を、利用するCodexの既存の `config.toml` へ追加する。
Plugin版を導入済みなら、別途同じサーバーを二重登録する前にPluginのツールを確認する。
設定を追加した後の実接続は、下の「接続後の確認」で確かめる。

## Claude Code

公式のstdio登録形式に従う。既存登録がある場合は重複追加せず現在の設定を確認する。
CLIで追加する場合も、生成したJSON内の `command` と `args` を使う。
以下のプレースホルダーを出力に合わせて置き換える。空白を含むパスは引用符で囲む。

```sh
claude mcp add --transport stdio --scope user anywhere-computer -- /absolute/path/to/python -B -I -X utf8 -m anywhere_computer mcp --state-dir /absolute/path/to/state
```

[Claude Code公式MCP手順](https://code.claude.com/docs/en/mcp)
を参照。Claude DesktopやWebの接続設定と同じ手順だとは扱わない。

## Gemini CLI

`settings.json`の`mcpServers`へ生成したJSONの項目を追加するか、CLIで登録する。
CLIの実行先と引数も、生成した設定に合わせる。

```sh
gemini mcp add --scope user --transport stdio anywhere-computer /absolute/path/to/python -B -I -X utf8 -m anywhere_computer mcp --state-dir /absolute/path/to/state
```

[Gemini CLI公式MCP手順](https://geminicli.com/docs/tools/mcp-server/)
を参照。既存設定をファイルごと上書きしない。Web版Geminiの対応をこの手順から推定しない。

## 接続後の確認

設定したインストール先と同じ状態保存先で `doctor` を実行し、エンジンの状態と
`catalog_state` を確認する。`catalog_unavailable` はエンジンが応答していても
ツール一覧を確認できなかった状態で、`doctor` は終了コード1を返す。起動を繰り返さず、
同じ保存先へのアクセスを確かめて診断を再実行する。

`doctor` が成功してもAIクライアントとの接続確認は別である。そのクライアント内で
ツール一覧を確認し、`computer_status`、使い捨てファイルの読取を実行する。
書込も必要ならそのファイルで試す。長時間処理や応答喪失は、
返された操作IDから `operations_get` で確認する。クライアント固有の認証・確認設定は
そのクライアントで管理し、Anywhere Computer独自の繰り返し承認を追加しない。
ローカルの `mcp` は既存エンジンを共有するため、クライアント終了とエンジン停止は別である。

登録構文の確認だけでは実接続・ツール実行は保証されません。
