# Anywhere Computer

[English](README.md) | 日本語

ChatGPTやCodexなどのMCPクライアントから、自分のPCを操作するための実行エージェントです。
接続したAIが作業を判断し、Anywhere Computerが指定されたPCでファイル・検索・文書・端末の操作を実行します。
ファイルや端末の基本機能にCodexのモデル実行は必要ありません。自作コードは[MIT License](LICENSE)です。

[リリースを入手](https://github.com/meteosimaji/anywhere-computer/releases) ·
[問題を報告](https://github.com/meteosimaji/anywhere-computer/issues) ·
[変更履歴](CHANGELOG.md)

公開済みの内容はリリースの配布物と検証記録で確認してください。
READMEはこのチェックアウトの案内です。開発版の機能が過去の公開版に含まれるとは限りません。
日付付きの試験記録は、そのときの版と環境での結果です。

<!-- BEGIN GENERATED: project-reference -->
このチェックアウトの情報（公開済み・稼働中の版を示すものではありません）:

| Source | Value |
| --- | --- |
| [Python package](src/anywhere_computer/__init__.py) | `0.2.0a13` |
| [Codex Plugin version mapping](scripts/package_plugin.py) | `0.2.0-alpha.13` |
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
ChatGPT接続には公開HTTPS URL・認証・ChatGPT側の登録も必要です。
日常の起動・診断・常駐設定は日本語の運用ガイドを参照してください。

## subchatで共同作業する

subchatは、役割を分担した通常のChatGPT Chatです。ChatGPT Workタスクとは別です。
親が作業範囲を指定し、複数の子から根拠付きの結果を集め、検証して統合する用途を目指します。
このチェックアウトのCodex Pluginは、能力・モデル一覧・保存済み操作・結果回収と、
保存済み回答のsandboxファイル1件を上限付きで取得する読み取り専用の7ツールを
既定で提供します。Pluginプロセスに`ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=browser-send`を
設定して再接続すると、ログイン済みの専用Chromeプロファイルを使う送信ツールが現れます。
Chromeは最小化しますが、macOSでの実測では一時的に前面へ出ました。能力表示では
`generation_transport=browser_prepared`となります。独立したHTTP専用の生成は
別途設定する`anywhere-subchat` CLI/MCPの実験機能で、実生成の合格はまだです。
既存のAnywhere ComputerエンジンとChatGPT向けリモート接続は、このSubchatサーバーと別です。
対応する通信経路・操作・検証結果・未対応事項は、上のsubchatガイドを正本とします。
Macのパスを書くだけでアクセス権が増えるわけではなく、選択したPCと接続ツールの権限が必要です。

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
