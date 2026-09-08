# Anywhere Computer

**Your computer, available through a dependable connection.**

ChatGPT・Codex などの MCP クライアントから、ファイル・検索・端末作業を行う
独立エージェント。Linux、Windows、macOS に共通の Python コードを使います。

開発中の **0.1.0 alpha** です。ローカルエージェントと 22 個の MCP ツールを
実装しています。TLS リモート通信と HTTP MCP の内部実装・ループバック試験もあります。
Word・Excel・PowerPoint の本文/セル読取を実装しています。
インターネット越しの実用接続、文書の編集/描画、GUI 操作は今後の開発対象です。

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

`doctor` は起動・停止・設定変更をせず、停止中、古い接続情報、資格情報ストアの
利用不可、プロセスは存在するが応答しない状態、別ビルドの稼働を区別します。
JSON の `state` と `action` に結果と対処を返し、`ready` 以外は終了コード 1 です。
`unresponsive` だけでは停止や認証失敗の原因は断定せず、作業を保持します。

既存の SSH 接続先に同じ版を導入している場合は、次の MCP 起動経路も使えます。
接続先の `anywhere` コマンド、認証設定、信頼済みホスト鍵が必要です。

```sh
anywhere remote-mcp --ssh-host windows-lab
```

## 複数端末

既存の SSH 設定のホスト名を登録します。秘密鍵・パスワードは登録情報に含めません。

```sh
anywhere device-add --name "Windows Lab" --ssh-host windows-lab
anywhere device-add --name "Linux Lab" --ssh-host linux-lab
anywhere devices
anywhere device-status --device <登録時に返されたID>
anywhere remote-mcp --device <登録時に返されたID>
anywhere device-rename --device <ID> --name "Windows Arm64"
anywhere device-remove --device <ID>
```

表示名を変更しても ID は維持されます。状態確認は明示的に実行します。
`ready` は確認した時点での応答を意味し、60 秒後は現在の状態を `unknown` とし、
最終確認結果と時刻を残します。失敗した確認は `unreachable` です。端末の電源断、
SSH 認証失敗、リモートコマンド失敗のどれかは、この結果だけでは断定しません。
`device-remove` はローカル登録の削除であり、SSH の認証権限の失効や
リモートエージェントの停止は行いません。

## 現在使えるもの

- Word 本文、Excel 保存済みセルと数式、PowerPoint スライド本文のページ読取。
- テキストの部分読取・複数読取、内容 hash、競合検知つき書込・限定置換、バックアップと復元。
- フォルダ一覧と作成、ファイル情報、同一 filesystem 内の通常ファイル移動。
- 非同期のファイル名・テキスト検索、結果のページ取得とキャンセル。
- 対話プロセスの起動、入力、出力 cursor、一覧、プロセス群の停止。
- 操作 ID の重複検査、結果照会、内容を含まない履歴一覧。
- クライアント切断に影響されないエージェントと端末セッション。
- 認証したローカル通信、OS 資格情報保管、起動時の状態診断。

[リモート通信の実装範囲と残作業](docs/REMOTE-TRANSPORT.md)

## 依存方針

実行時の直接依存は現在 pydantic・psutil・keyring の3つです。
MCP の stdio 接続、TLS 通信、プロセス間ロック、保存先判定は独自実装です。
公式 MCP SDK は互換性試験用の開発依存にのみ含めます。起動時に自動更新せず、
uv.lock の固定した組合せで検証します。残る依存の削減も段階的に進めます。

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

`plugins/anywhere-computer` にローカル Codex 用 alpha パッケージを用意しています。
uv と利用可能な OS 資格情報ストアが必要です。初回は Python と固定依存の取得に
ネットワークを使います。キャッシュ準備後のオフライン起動は別途検証しています。
ソースを変更した際は `uv run python scripts/package_plugin.py` で同梱 wheel を
更新してください。pytest が同梱コードとソースの一致を検査します。

エージェントは実装の hash を返します。別ビルドへ更新する際、実行中の作業が
あれば停止せず理由を返します。作業がない場合だけ旧ビルドを終了して切り替えます。
