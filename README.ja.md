# Anywhere Computer

[English](README.md) | 日本語

ChatGPTやCodexなどのMCPクライアントから、自分のPCを操作するための実行エージェントです。
接続したAIが作業を判断し、Anywhere Computerが指定されたPCでファイル・検索・文書・端末・隔離されたヘッドレスブラウザの操作を実行します。
ファイルや端末の基本機能にCodexのモデル実行は必要ありません。自作コードは[MIT License](LICENSE)です。

[リリースを入手](https://github.com/meteosimaji/anywhere-computer/releases) ·
[問題を報告](https://github.com/meteosimaji/anywhere-computer/issues) ·
[変更履歴](CHANGELOG.md)

公開済みの内容はリリースの配布物と検証記録で確認してください。
READMEはこのチェックアウトの案内です。開発版の機能が過去の公開版に含まれるとは限りません。
過去の試験記録は、このリポジトリの外で保管しています。

<!-- BEGIN GENERATED: project-reference -->
このチェックアウトの情報（公開済み・稼働中の版を示すものではありません）:

| Source | Value |
| --- | --- |
| [Python package](src/anywhere_computer/__init__.py) | `0.2.0a67` |
| [Codex Plugin version mapping](scripts/package_plugin.py) | `0.2.0-alpha.67` |
| [Python requirement](pyproject.toml) | `>=3.12` |

各項目の正本:

- [配布物と導入](docs/PORTABLE.md)
- [MCPクライアントの設定](docs/MCP-CLIENTS.md)
- [対話式設定とChatGPT接続](docs/SETUP-CONTROLLER.md)
- [コマンドと診断](docs/OPERATIONS.md)
- [更新と復旧](docs/UPDATING.md)
- [複数PCの操作](docs/DEVICE-ROUTING.md)
- [GUIの導入条件と制限](docs/GUI-MCP.md)
- [subchatの使い方と制限](docs/SUBCHAT-PROBE.md)
- [今後の開発方針](docs/PRODUCT-ROADMAP.md)
- [文書の正本と更新方法](docs/DOCUMENTATION.md)
<!-- END GENERATED: project-reference -->

## 使い始める

Python同梱版は、リリースからOSに合った実行用ZIPを取得し、上の配布ガイドに従います。
GitHubの「Download ZIP」はソースコードです。CodexへのPlugin導入だけではChatGPTとの接続は完了しません。

ソースからの最短手順とローカルMCP設定は[英語READMEのGet started](README.md#get-started)を正本としています。
コマンド列をこの翻訳へ重複掲載せず、同じ手順を参照します。OS資格情報ストアが必要で、
現在のChatGPT接続手順には公開HTTPS URL・認証・ChatGPT側の登録が必要です。
ChatGPTからこのMacの`localhost`へ直接接続はできません。所有ドメインを使わない
非公開の開発用接続には[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
という選択肢がありますが、本プロジェクトでは未検証です。公開Pluginの提出には
安定した公開HTTPSエンドポイントが必要です。
日常の起動・診断・常駐設定は日本語の運用ガイドを参照してください。
Codex Pluginの導入にはCodexから使える`uv`が必要です。このチェックアウトを使う場合は
[英語READMEの導入コマンド](README.md#get-started)でローカルmarketplaceを登録し、
`anywhere-computer`を導入します。再接続後、独立した`anywhere-computer`と
`anywhere-subchat`のMCPサーバーを確認し、`subchat_capabilities`で実際の版とモードを確認します。

## subchatに独立した調査を委任する

subchatは、役割を分担した通常のChatGPT Chatです。ChatGPT Workタスクとは別です。
親が作業範囲を指定し、複数の子から根拠付きの結果を集め、検証して統合する用途を目指します。
子Chat同士は途中結果を交換しません。独立した担当には並列化が役立ちますが、
重複する調査では作成と回収の時間が増えます。密接に関連する調査は単一Chatで進めます。
このチェックアウトのCodex Pluginは、能力・モデル一覧・保存済み操作・結果回収と、
保存済み回答のsandboxファイルと、保存済み会話に結び付く生成画像を
上限付きで取得する読み取り専用ツールを
提供します。macOSでログイン元プロファイルとアカウントIDを選択・固定し、
`subchat/login-selection.json`に`"enable_background_send": true`を明示した場合は、
起動時に背景Chrome準備とHTTPX送信のツールも提供します。明示的な選択が
未設定なら、読み取り専用が既定です。Windowsでは専用のChromeまたはEdgeで
ログインし、`anywhere-subchat-setup choose-dedicated`でブラウザー、アカウントID、
送信同意を保存できます。手順と実機検証の範囲は
[Subchatガイド](docs/SUBCHAT-PROBE.md#windows-dedicated-chrome-or-edge-profile)を参照してください。
固定済みでも読み取り専用にする場合は
`ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=http-read-only`を設定します。
macOSでは`anywhere-subchat-setup inspect PROFILE`で背景スナップショットから
アカウントIDを確認し、`select`でプロファイルとIDを保存できます。具体的な指定方法は
[Subchatガイド](docs/SUBCHAT-PROBE.md)を参照してください。
Chromeの保護対象ファイルを読めないHTTPSのLaunchAgentには、サービス停止中に
`anywhere-subchat-setup stage PROFILE --expect-account-id ID --http-state-dir DIR`で
選択済みログインをアプリの状態ディレクトリへ保存できます。手順は
[HTTPサーバーの設定](docs/HTTP-SERVER.md)を参照してください。
Pluginプロセスに`ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=browser-send`を
設定して再接続すると、ログイン済みの専用Chromeプロファイルを使う送信ツールが現れます。
Chromeは最小化しますが、macOSでの実測では一時的に前面へ出ました。能力表示では
`generation_transport=browser_prepared`となります。独立したHTTP専用の生成は
別途設定する`anywhere-subchat` CLI/MCPの実験機能で、実生成の合格はまだです。
既存のAnywhere ComputerエンジンとChatGPT向けリモート接続は、このSubchatサーバーと別です。
macOSでは、選択したログイン済みChromeプロファイルのSubchatツールをHTTPS MCPにも
明示的に追加できます。既存の接続には自動で権限を付けず、新しいOAuth認可が必要です。
設定方法と送信IDの回収手順は[HTTPサーバーの設定](docs/HTTP-SERVER.md)を参照してください。
macOSでは`ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=browser-prepared-httpx`を選ぶと、
指定したログイン済みChromeプロファイルの一時スナップショットで送信準備を行い、
生成POSTだけをHTTPXで一回送ります。GPT-6 Proの新規送信と同一会話への追送で、
両方の生成POSTがHTTPX `200 text/event-stream`となり、回答保存まで確認しました。
別の画像生成Chatでは、送信操作に結び付いたPNG画像をMCP経由で取得できました。
画像の取得と最終回答の確認は別の状態として返します。
30fpsの録画とフォーカス記録では専用Chromeの前面化はありませんでした。
準備には背景Chromeが必要で、どの環境でも前面化しない保証ではありません。
`ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE`でプロファイルを選び、複数アカウントを
使う場合は`ANYWHERE_SUBCHAT_EXPECTED_ACCOUNT_ID`で対象を固定してください。
対応する通信経路・操作・検証結果・未対応事項は、上のsubchatガイドを正本とします。
Macのパスを書くだけでアクセス権が増えるわけではなく、選択したPCと接続ツールの権限が必要です。

隔離ブラウザには、所有するタブで一意の可視要素を対象にする`browser_click`と
`browser_fill`もあります。操作後の確認に失敗した場合は結果不明を返すので、
同じ操作を繰り返す前にタブを観測してください。既存Chromeのプロファイルや
タブへの接続は、この隔離ブラウザ機能には含まれません。

ローカルのエージェント同士で明示的に文章を送る`anywhere-peer` MCPもあります。
`anywhere-peer --help`に所有者による登録と起動方法を示します。各peerの資格情報は
権限0600の別ファイルで持ち、同じ所有者・アカウント・プロジェクトに限定します。
送信先が接続している必要があります。配送と受領を記録しますが、モデルのターンを
開始したり、ChatGPT・Codex・Claudeの会話本文へ自動挿入したりはしません。

通常ChatへのHTTPアクセスは、本人に認められたアカウントと利用範囲でのみ使ってください。
モデル蒸留を目的とする大量取得や第三者へのアクセス販売などの不正利用は禁止です。
この実験的経路はOpenAIが文書化したAPIではありません。許可範囲外の利用で
アカウントの制限・停止などが生じても、Plugin作成者はその責任を負いかねます。
使用前に許可の正確な条件と適用される規約を確認してください。

## 開発と文書の更新

実行環境はリポジトリの`uv run --locked`を使います。必要な検査は
[Qualityワークフロー](.github/workflows/quality.yml)で確認してください。
版番号と共通リンクは自動生成し、CIが更新漏れと参照先ファイルの欠落を検査します。
正本の場所と更新コマンドは、上の「文書の正本と更新方法」にまとめています。
機能の説明や実機検証の正しさまで、この検査だけで保証するものではありません。

問題報告にはOS／CPU、実際に稼働している版、再現手順、診断状態を添えてください。
パスワード・トークン・個人ファイルの内容は含めないでください。
操作の応答が途切れた場合は、新しい操作IDで繰り返す前に元のIDで結果を回収します。
