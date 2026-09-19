# Anywhere Computer

[English](README.md) | 日本語

開発ブランチは現在 `0.2.0a1` です。公開済み beta 1 の配布物とは異なります。
[0.2〜1.0の方針](docs/PRODUCT-ROADMAP.md)と
[CoS比較・適用する修正](docs/CHAT-ON-STEROIDS-REVIEW-2026-09-18.md)を参照してください。

ChatGPT・CodexなどのMCPクライアントから、自分のPCのファイル・検索・文書・端末を扱うための、セルフホスト型の実行エージェントです。
判断は接続したAIが行い、PC上のAnywhere Computerが操作を実行します。ファイルや端末の基本機能にCodexのモデル実行は必要ありません。

最初のベータ公開版は `0.1.0b1`、Codex Pluginの表記は `0.1.0-beta.1` です。
[リリースとダウンロード](https://github.com/meteosimaji/anywhere-computer/releases)。
既存のファイル・検索・pipe端末・文書抽出・Skills・MCP／端末ルーティングを固定したベータです。
正式stable版ではありません。[ベータの対応範囲と受け入れ状況](docs/BETA1.md)を確認してください。

## できること

| 機能 | ベータでの対応 |
|---|---|
| ファイル | 一覧・検索・部分読取・書込・限定置換・移動・バックアップ・復元 |
| ファイル受け渡し | 分割アップロード／ダウンロード、ハッシュ確認、中断後の再開 |
| 文書 | Word・Excel・PowerPointのテキスト／セル読取、単純なDOCX・XLSX生成 |
| 端末 | コマンド起動、追加入力、出力の続きの取得、一覧、停止。現在はpipe方式 |
| 作業の継続 | クライアント切断後のセッション維持、操作IDによる結果回収・重複防止 |
| ChatGPTとの接続 | 認証付きHTTP MCP、再接続、認証更新、任意のCloudflareトンネル連携 |
| Codexとの連携 | 選択した会話・スキルの参照、対応Pluginの呼び出しとセッション保持 |
| 共通エンジン | Chat側のHTTP入口とCodex側のローカル入口から同じエンジンを利用可能 |

組込みのネイティブGUIと、Plugin標準のブラウザー操作は提供しません。macOSのGUIは別途導入したPeekaboo MCPへ接続します。独立ブラウザーは開発中で、直接MCP経由の隔離ブラウザー操作まで検証していますが、既存タブ・ログイン状態の利用や公開ツールへの組込みは未完了です。文書の描画や書式を保った編集、PTYも今後の対象です。

Codexのツールを呼び出せることは、Codex専用の画面操作文脈も利用できることを意味しません。既知の非対応Computer Use経路は、実行前に理由を返します。[Codex連携の対応範囲](docs/CODEX-CONTEXT.md)

Claude Code・Gemini CLIなどの接続設定と検証範囲は[MCPクライアント接続ガイド](docs/MCP-CLIENTS.md)を参照してください。

alpha9に含まれるGUIアダプターでは、独立した既存MCPを使う型付きGUI操作を検証しています。macOSのPeekabooで、
認証付きHTTPから電卓の連続操作と結果回収を確認しました。ChatGPTのGPT-5.6 Sol（中程度）でも
観測→Escape→入力→再観測で42を確認済みです。[修正・更新検証記録](docs/UPDATE-VERIFICATION-ALPHA8-2026-09-13.md)。
導入条件と、
更新後のツール追加手順は[GUI用MCP接続ガイド](docs/GUI-MCP.md)を参照してください。

alpha9は、同じChatGPT接続から登録済みの別PCを明示指定するHTTP端末ルーターを追加しています。
Windows VMも、そのゲスト内のAnywhere ComputerをSSHまたは認証付きHTTPで登録する操作先です。
接続方法と機械試験・実機試験の区別は[複数端末の操作](docs/DEVICE-ROUTING.md)を参照してください。
ChatGPTからWindows VMのファイル作成・条件付き追記・端末実行・
結果回収まで実機検証しています。GUIとVM再起動後の自動復旧は別の確認項目です。
[Windows実操作の検証記録](docs/research/2026-09-13-windows-owner-pipe-acceptance.md)

## 入手方法

このリポジトリは[GitHubで公開](https://github.com/meteosimaji/anywhere-computer)しています。ソースは任意のタイミングで取得できます。

```sh
git clone https://github.com/meteosimaji/anywhere-computer.git
cd anywhere-computer
```

[GitHub Releases](https://github.com/meteosimaji/anywhere-computer/releases)からOS別のPython同梱ZIPとCodex Plugin ZIPを取得できます。GitHubの「Download ZIP」はソース一式であり、Python同梱の実行用ZIPとは異なります。実行用ZIPはCIで生成・検証しており、配布構造とビルド方法は[ランタイム同梱配布物](docs/PORTABLE.md)にまとめています。

## ソースから起動する

[uv](https://docs.astral.sh/uv/)と、利用可能なOS資格情報ストアが必要です。Python 3.12と固定依存を準備してから起動します。

```sh
uv sync --locked --python 3.12
uv run --locked anywhere start
uv run --locked anywhere status
```

macOSはKeychain、WindowsはWindows Credential Manager、LinuxはSecret Service／KWalletなどを使用します。Linuxのheadless環境でも、利用可能で解錠済みの資格情報ストアが必要です。

MCPクライアントには、このリポジトリを作業ディレクトリとして次のコマンドを設定します。

```sh
uv run --locked anywhere mcp
```

この入口は、エージェントが停止していれば起動します。通常接続は互換性のある稼働エンジンを再利用し、接続しただけで別ビルドへ置き換えません。

```sh
uv run --locked anywhere doctor
uv run --locked anywhere stop
```

`doctor`は診断用です。`stop`は作業中なら停止せず、その理由を返します。起動・常駐登録・停止・複数端末の詳細は[運用ガイド](docs/OPERATIONS.md)を参照してください。

## 対話式コマンド入口（開発中のmain向け）

開発版では `anywhere setup`（ソースからは `uv run --locked anywhere setup`）で、
ローカル起動・ChatGPT向けHTTPS設定・ネイティブOAuth向けHTTPS設定を選択できます。
再開時は同じ `--state-dir` を指定します。選択前の取消しは設定を変更しません。
ローカル経路は `start` と同じく、待機中のエンジンを現在のインストールへ切り替える
場合があります。中継・公開URLの作成やAI側の登録は自動化しません。
この入口は以前公開したalpha9 ZIPには含まれません。

## ChatGPTから接続する

CodexへのPluginインストールだけでは、ChatGPT側の接続は完了しません。実行環境を用意した後、ChatGPT用の設定を開始します。

```sh
uv run --locked anywhere chatgpt-setup --state-dir ./local-state/chatgpt
```

公開HTTPSのMCP URLと認証設定が必要です。ChatGPT用のクライアントIDと戻り先は自動入力されます。パスワード・トンネル資格情報はローカルの非表示入力で設定し、中断した場合は同じコマンドで再開できます。

セットアップ完了時に表示される起動・診断コマンドを使い、そのMCP URLをChatGPT側に登録して認証します。公開URL・DNS・トンネルの作成、OSの常駐登録、ChatGPT側の追加までをすべて自動完了するインストーラーではありません。

- [ChatGPT用セットアップ](docs/SETUP-CONTROLLER.md#chatgpt-preset-and-issuer-identification)
- [HTTPサーバーと認証](docs/HTTP-SERVER.md)
- [任意のCloudflareトンネル連携](docs/CLOUDFLARE-TUNNEL.md)
- [ログイン時の常駐起動](docs/PERSISTENCE.md)

接続認証と操作ごとの確認は別です。接続するAIクライアント側の権限設定を使い、Anywhere Computer独自の毎回の操作承認画面は追加しない方針です。OSや外部サービスが必要とする認証・権限設定は残ります。

## 更新と接続の引き継ぎ

更新は利用者が選んだタイミングで行います。alpha9にはstable更新用コマンドを含みますが、正式stable候補はまだありません。自動更新は既定で無効です。公開beta 1は手動で取得します。alpha／betaはstable更新の対象外です。希望する利用者だけが将来のstable自動更新を明示的に有効化します。

現在は、更新したインストールから `anywhere start` を実行すると、作業がない場合にエンジンを切り替えます。状態保存先を変えなければ、資格情報・設定・操作履歴を引き継ぎます。稼働中の端末やPluginセッションは、切替前に完了・終了する必要があります。

常駐サービスの登録も新しい実行場所へ移す場合は、同じHTTP設定ディレクトリを指定して `autostart-upgrade`、続いて `autostart-start` を実行します。新しい版がその場所を使うため、選択中の実行フォルダーを削除・移動しないでください。

`anywhere update` は、stable配布物の検証・待機・中断復旧をまとめるコマンドです。alphaリリースは自動適用しません。[更新の対応状況と手順](docs/UPDATING.md)

macOSでは、共通エンジンへの移行と更新後に、既存のHTTP接続から再認証なしで接続し、過去の操作結果を回収できることを確認しています。ただし、既に起動しているすべてのCodexタスクが自動でPluginを再読込するとは限りません。[更新・再接続の検証記録](docs/UPDATE-VERIFICATION-2026-09-12.md)

## 対応環境と確認範囲

共通のPythonコードをmacOS・Windows・Linuxで使います。同梱ランタイムはOS／CPU別です。

| 確認対象 | 確認できている範囲 |
|---|---|
| macOS | 実エージェント、認証付きHTTP、端末再接続、常駐版更新、共通エンジン移行 |
| Windows VM | ChatGPTから明示端末指定でファイル・端末・結果回収、更新後の保存データ継続、インストール済みPluginコマンド実行 |
| Windows・Ubuntu CI | 共通テスト、配布ZIP生成、展開後のランタイム試験、配布物の出所証明検証 |
| Linux ARM64 | 過去の隔離ゲストで、Secret Serviceを使った実エージェント試験 |
| 全OSの初回導入・再ログイン・スリープ復帰 | 一般利用者の環境での受け入れは未完了 |

CI成功は、すべての実機でGUI操作・ログイン後の自動接続ができることの証明ではありません。詳細は[配布ガイド](docs/PORTABLE.md)と[実装記録](docs/EXECUTION-ROADMAP.md)を参照してください。

## 困ったとき

| 状態 | 最初に確認すること |
|---|---|
| コマンドが見つからない | 端末と常駐環境のPATHの違い。選択したPython・uv・接続子の絶対パスを使う |
| 起動できない | `anywhere doctor` の `state` と `action`、OS資格情報ストアの利用可否 |
| ローカルは動くがChatからつながらない | 同じHTTP設定を使った `remote-doctor --probe-public` とChat側の認証 |
| 更新できない | 実行中の端末・Plugin・検索などが残っていないか |
| 操作の応答が途切れた | 同じ操作を新しいIDで繰り返す前に、既知のIDで `operations_get` を確認する |
| GUI操作が拒否される | Peekabooの導入・OS権限・対応schemaを確認。Codex専用の非対応経路は拒否ガードを外しても利用可能にはならない |

報告にはOS／CPU、版、再現手順、診断の状態を添えてください。パスワード・トークン・個人ファイルの内容は含めないでください。[Issues](https://github.com/meteosimaji/anywhere-computer/issues)

## 開発

基本実行時の直接依存はpydantic・psutil・keyringです。直接MCPとブラウザー向けの任意依存は別に定義しています。サーバー側の既存MCP入口は独自実装で、直接MCPクライアントは公式MCP SDKを使います。

変更に対応する小さなテストから実行し、影響範囲に応じて検査を広げます。

```sh
uv run --locked pytest tests/test_connection.py -q
uv run --locked ruff check src tests scripts
uv run --locked mypy
uv run --locked pytest -q
uv build
```

本体ソースを変更した場合は、検証前に同梱Pluginを再生成します。

```sh
uv run --locked python scripts/package_plugin.py
```

同梱wheelとソースの一致はテストで確認します。詳細な運用仕様・設計・検証記録は次を参照してください。

- [構成と制限](docs/ARCHITECTURE.md)
- [製品要件](docs/PRODUCT.md)
- [5段階の実装記録](docs/EXECUTION-ROADMAP.md)
- [変更履歴](CHANGELOG.md)
- [転送と再開](docs/BINARY-TRANSFER.md)・[アップロード](docs/UPLOADS.md)
- [検索](docs/SEARCH.md)・[複数端末](docs/DEVICE-ROUTING.md)

## ライセンス

自作部分は[MIT License](LICENSE)です。同梱Python・第三者ライブラリにはそれぞれのライセンスが適用され、その表示を配布物内に保持します。
