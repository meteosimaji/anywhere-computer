# MCPクライアントから利用する

Anywhere Computerの基本機能は標準MCPを入口にする。Codexのモデル実行は必要ない。
各AIクライアント自身の利用料金・利用枠は、その提供元の規則に従う。

## ローカルの共通設定

先にリポジトリで `uv sync --locked --python 3.12` を実行する。
設定にはuvとリポジトリの絶対パスを使う。PATHの異なるデスクトップ起動でも
同じ実行先を選べる。以下のプレースホルダーは実際のパスに置き換える。

```json
{
  "mcpServers": {
    "anywhere-computer": {
      "command": "/absolute/path/to/uv",
      "args": [
        "run", "--locked", "--directory", "/absolute/path/to/anywhere-computer",
        "anywhere", "mcp"
      ]
    }
  }
}
```

追加のMCPサーバーをAnywhere Computerから直接呼ぶ開発版機能を利用する場合は、
`uv sync --locked --python 3.12 --extra mcp` で追加依存を準備する。
通常のファイル・端末操作にはCodexのインストールは不要。Codex会話・登録済みPlugin・
Skillsの取得は任意の連携であり、Codexがない場合はその呼び出しが失敗する。

## Claude Code

公式のstdio登録形式に従う。既存登録がある場合は重複追加せず現在の設定を確認する。

```sh
claude mcp add --transport stdio --scope user anywhere-computer -- /absolute/path/to/uv run --locked --directory /absolute/path/to/anywhere-computer anywhere mcp
```

[Claude Code公式MCP手順](https://code.claude.com/docs/en/mcp)
を参照。Claude DesktopやWebの接続設定と同じ手順だとは扱わない。

## Gemini CLI

`settings.json`の`mcpServers`へ上の共通設定を追加するか、CLIで登録する。

```sh
gemini mcp add --scope user --transport stdio anywhere-computer /absolute/path/to/uv run --locked --directory /absolute/path/to/anywhere-computer anywhere mcp
```

[Gemini CLI公式MCP手順](https://geminicli.com/docs/tools/mcp-server/)
を参照。既存設定をファイルごと上書きしない。Web版Geminiの対応をこの手順から推定しない。

## 接続後の確認

ツール一覧を確認し、使い捨てファイルへの書込・読取を実行する。長時間処理や応答喪失は、
返された操作IDから `operations_get` で確認する。クライアント固有の認証・確認設定は
そのクライアントで管理し、Anywhere Computer独自の繰り返し承認を追加しない。
ローカルの `mcp` は既存エンジンを共有するため、クライアント終了とエンジン停止は別である。

登録構文の確認だけでは実接続・ツール実行は保証されません。
