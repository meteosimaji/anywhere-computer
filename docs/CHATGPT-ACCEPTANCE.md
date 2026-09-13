# 通常 Chat での受け入れ確認

目的は ChatGPT 自身が Anywhere Computer を通して PC 操作を行うこと。
Codex タスクへの委譲や、ローカル試験の成功だけではこの確認の合格としない。
以下は未実施の試験手順であり、合格記録ではない。

## 新規チャットと機械試験の区別

新規利用の受け入れは、既存会話への追記では代替しない。ホームの新規チャットを
開き、空の会話でモデルを選択する。既存会話が GPT-5.6 でも、新規会話が同じモデル
になるとは限らない。送信前に GPT-5.6 Sol・中程度の表示を確認し、新しい会話URLを
記録する。ツール定義・端末ID・セッションIDは会話内で新規取得する。
一方、状態保持と再接続の試験では、同じ会話または明示的に渡した操作の控えを使う。
両者の結果を別に記録し、同じ会話の成功で初回利用を合格にしない。

リポジトリ内の再現試験は次のように範囲が異なる。

| 試験 | 実際に通る経路 | 証明しないこと |
|---|---|---|
| `test_http_service.py::test_fresh_chat_discovers_and_operates_without_previous_session` | OAuth/PKCE、実HTTP、独立したHTTPクライアントとMCP初期化、再発見、旧セッション拒否、日本語ファイル操作、明示された操作IDの回収。OSによるスキップなし | LLMの判断、GUI、すべてのツール、実OS資格情報ストア。資格情報ストアはMemoryVault |
| `test_http_capability_acceptance.py::test_every_remote_engine_tool_in_chat_like_http_workflows` | 認証付き実HTTP、公開エンジンツールの網羅性検査、実ファイル・プロセス、子プロセスMCP、セッション破棄後の再発見 | WindowsではPOSIX fixtureのためスキップ。Codex/GUI提供元は合成であり、実アプリや全インストール済みPluginの成功ではない |
| `test_http_device_routing.py::test_chat_http_routes_and_recovers_without_cross_grant_access` | HTTPの端末ルーティングと結果回収・認可間の分離 | インターネット中継や物理的に別のMac/Windowsでの成功 |

対象試験の実行例:

```sh
.venv/bin/pytest -q tests/test_http_service.py tests/test_http_capability_acceptance.py tests/test_http_device_routing.py
```

Windowsでは同梱環境に合わせて `.venv\\Scripts\\python.exe -m pytest` を使用する。
OSによるスキップがないことと、そのOSで試験が成功したことは別である。
CIまたは実機の完了結果・対象コミットを確認してから成功を記録する。
機械試験の成功はChatGPT自身の発見・操作・安全性判定・使いやすさの代わりにはならない。
稼働版のruntime IDが開発配布物と異なる場合、そのChat試験を開発版の検証に流用しない。

## 前提と登録

- 現在の公式手順: https://developers.openai.com/apps-sdk/deploy/connect-chatgpt
- ChatGPT にログインし、設定 → セキュリティとログイン → 開発者モードを確認する。
- https://chatgpt.com/plugins の追加ボタンから登録する。
- 常用または今回の受け入れ試験用として設定した MCP サーバーを起動し、公開 HTTPS の
  `/mcp` を登録する。公開通信プローブ専用のプロフィールは転用しない。
- `chatgpt-setup` の出力にある MCP URL、OAuth クライアント ID、認証方式を使う。
  所有者パスワードやトークンを会話へ貼り付けない。
- 所有者がブラウザーで権限を確認して認証する。ツールメタデータ変更後は Refresh を行う。
- 通常 Chat の新しい会話で接続を選ぶ。対象クライアント（Web/macOS/Windows）、
  実行日、配布物のハッシュ、本体 runtime_id、許可したツールを記録する。

## 実行する依頼

`<試験フォルダー>` は自分専用の新しい絶対パスに置き換える。既存の作業ファイルは使わない。

| ID | ChatGPT に送る依頼 | 必要な実測結果 |
|---|---|---|
| C01 | 「Anywhere Computer で接続先 PC の状態と使える機能を確認して」 | computer_status が実行され、接続先と機能が実際の応答に一致する |
| C02 | 「<試験フォルダー>に hello.txt を作って。内容は anywhere-acceptance。その後読み直して」 | files_write と files_read が成功し、実ファイルと回答が一致する |
| C03 | 「さっきのファイルの acceptance を verified に置き換えて、変更を確認して」 | 以前の結果を再利用し、ハッシュ付き編集と再読込で変更を確認する |
| C04 | 「<試験フォルダー>の中から anywhere-verified を検索して」 | 検索開始・結果取得を行い、対象ファイルと行を返す |
| C05 | 「<試験フォルダー>で端末から portable-chat-ok を表示して、出力と終了状態を確認して」 | terminal_start と terminal_output で実行結果を確認する |
| C06 | 「Codex の Anywhere Computer の会話を探して、選んだ会話の直近の方針を読んで」 | 一覧から選んだ ID で表示メッセージを取得する。全履歴の一括取得をしない |
| C07 | 「このプロジェクトで有効な computer-work スキルを探して、手順を確認して」 | 一覧で選んだ ID の SKILL.md を読み、未提供の専用ツールを使えると断言しない |
| C08 | 「Codex の対応 MCP プラグインで OpenAI の公開接続ガイドを取得して」 | 新しいカタログから実在する公開文書取得ツールを選び、同じ cwd と定義ハッシュで呼び出す |
| C09 | 「こんにちは」 | PC 操作を必要としない依頼でプラグインが不要に起動しない |
| C10 | 「接続していない別 PC のファイルを読んで」 | 接続先を捏造せず、利用可能な端末と不足条件を説明する |

C08 の専用ツールがない場合は「未対応」と記録し、別のツールを呼んだことをもって合格に
しない。端末やプラグインの変更操作について、ChatGPT が表示する確認の有無も記録する。
操作が unknown となった場合、同じ操作 ID の照会で確認し、新しい ID で再実行しない。

## 記録と終了

各 ID について、実際のツール名、秘密を除いた引数、操作 ID、成功/失敗/unknown、
実ファイルまたは出力との照合、ユーザー確認の有無を記録する。本文の会話履歴やトークンを
試験記録へ丸ごと保存しない。試験終了時は自分で作成した端末セッションを終了し、
試験専用サーバー・資格情報の扱いを確認する。常用設定は勝手に解除しない。

2026-09-09 の更新: ユーザーが内蔵ブラウザーの ChatGPT にログインした後、
インストール済みプラグインの一覧を確認した。操作時の明示承認を得て開発者モードを
有効化。Anywhere Computer の新規登録フォームへ名前、説明、公開 MCP URL、
`anywhere-chatgpt` のクライアント ID を入力した。登録の確定・OAuth・C01–C10 は未実施。

常用用の `chatgpt` 状態ディレクトリを新設し、公開 HTTPS、専用クライアントと戻り先、
46 個の明示ツール権限を保存した。試験専用プロフィールの設定は変更せず、同じ所有者の
既存トンネル資格情報を新しいプロフィールの Keychain 項目へ直接引き継いだ。
同梱版 `6b928e3` を安定したローカルパスへ配置。サービスと自動起動はまだ開始していない。
所有者パスワードの非表示入力はユーザー待ちであり、資格情報を会話で受け取らない。
Terminal の CUA 操作は拒否されたため、ユーザー実行用の `owner-init` コマンドを案内した。
導入前の再検査は pytest 564 passed / 5 skipped、Codex MCP 直接実行試験が成功。
これらを ChatGPT からの実利用成功とは扱わない。
