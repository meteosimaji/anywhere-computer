# Anywhere Computer

**Your computer, available through a dependable connection.**

ChatGPT・Codex などの MCP クライアントから、ファイル・検索・端末作業を行う
独立エージェント。Linux、Windows、macOS に共通の Python コードを使います。

開発中の **0.1.0 alpha** です。ローカルエージェントと 20 個の MCP ツールを
実装しています。TLS リモート通信の内部実装とループバック試験もあります。
インターネット越しの実用接続、Office、GUI は今後の開発対象です。

## 起動

Python 3.12 と [uv](https://docs.astral.sh/uv/) を利用します。

```sh
uv sync --locked --python 3.12
uv run anywhere start
uv run anywhere status
```

MCP クライアントには、このリポジトリを作業ディレクトリにして
`uv run --locked anywhere mcp` を設定します。エージェントが停止していれば起動します。
資格情報は OS の保管機能を利用します。Linux では Secret Service/KWallet などの
利用可能な保管機能が必要です。

```sh
uv run anywhere doctor
uv run anywhere stop
```

実行中の端末セッションがある場合、`stop` は停止せず理由を返します。
ログイン時の自動起動はまだ設定しません。

## 現在使えるもの

- テキストの部分読取・複数読取、内容 hash、競合検知つき書込・限定置換、バックアップ。
- フォルダ一覧と作成、ファイル情報、同一 filesystem 内の通常ファイル移動。
- 非同期のファイル名・テキスト検索、結果のページ取得とキャンセル。
- 対話プロセスの起動、入力、出力 cursor、一覧、プロセス群の停止。
- 操作 ID の重複検査、結果照会、内容を含まない履歴一覧。
- クライアント切断に影響されないエージェントと端末セッション。
- 認証したローカル通信、OS 資格情報保管、起動時の状態診断。

[リモート通信の実装範囲と残作業](docs/REMOTE-TRANSPORT.md)

## 開発

```sh
uv run ruff check src tests
uv run mypy
uv run pytest -q
uv build
```

[共通ランタイムの構成と制限](docs/ARCHITECTURE.md) / [製品要件](docs/PRODUCT.md)

## 配布

private リポジトリで開発しています。GitHub 配布と ChatGPT/Codex の公式公開
ディレクトリへの掲載を目標にしています。現時点では公開申請していません。
自作コードは [MIT](LICENSE) です。
