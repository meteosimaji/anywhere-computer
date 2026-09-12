# 直接MCPの開発版検証 — 2026-09-12

この記録は未コミットのローカル開発版に対するものです。公開alpha 6、稼働中の
Chat接続、一般利用者の環境での完了を示すものではありません。

## 登録済みツールからのブラウザー実操作

macOS上で、Codexを起動せず、Engine.executeの通常の登録ツール経路を使いました。
別のモデル生成は開始していません。Playwright MCP 0.0.80は試験用の一時ディレクトリ
に置いたもので、製品への同梱・依存採用とは区別します。

1. `mcp_session_open`でNode.jsからPlaywright MCPを起動。
2. `mcp_tools`で`browser_click`を含むカタログを取得。
3. `mcp_call`から`browser_navigate`を実行し、専用のloopback HTTPページへ移動。
4. サーバーが返したスナップショットファイルを`files_read`で読み、
   「Confirm fixture」ボタンの参照`e2`を取得。
5. `browser_click`にその参照を渡し、`browser_snapshot`で「Confirmed 42」を確認。
6. スナップショット操作の結果が台帳に保存されていることを照合。
7. `mcp_session_close`で終了確認を取得し、`update_blocked=false`を確認。

ブラウザーはheadless・isolated・sandbox指定の専用インスタンスです。既存の個人タブ、
ログイン状態、拡張機能による接続は使用していません。テスト用HTTPサーバーとエンジンは
終了しています。

## 画像とセッションの回帰試験

実stdio MCPサーバーから合成PNGを返し、登録された`mcp_call`経由で取得しました。
返却コンテンツは公式SDKの`ImageContent`として検証し、`operations_get`で回収した
結果も同じ画像になることを確認しています。構造化データに重複した画像は送信時のみ
サイズ・SHA-256へ置き換え、台帳の元データは保持します。

セッション所有者の分離、操作IDの再送による重複防止、同時起動上限、アイドル終了、
実行中の呼び出しの保護、未確認の後片付けを更新待機に残す仕組みは、対応する
`test_direct_mcp*`と`test_plugin_image_results.py`で検査します。

## 開発版の依存関係

ソースから直接MCPを利用する場合は `uv sync --locked --extra mcp` で任意依存を
導入します。基本Pythonパッケージは引き続きSDKを必須にしません。開発中のPlugin
配布処理はMCP SDK 1.30.0をハッシュ付き依存一覧に含めます。Playwright MCP、Node.js、
ブラウザー本体の同梱は別の未完了項目です。

## 残る受け入れ

- 認証付きHTTPと実際のChat画面を通した画像表示・ブラウザー操作。
- 既存ブラウザーのタブとログインを利用する接続・初回設定。
- 任意依存とブラウザー実行環境を含む配布・導入手順。
- Windows/Linuxでの直接MCPと子プロセス終了の実機検証。

本記録はこれらの完了や、Codex全機能との同等性を主張しません。

## 既存ブラウザー経路の調査

Playwright MCP 0.0.80のインストール済み実装と、公式拡張機能READMEを確認しました。
`--extension`はChrome/Edgeの既存ブラウザーへ接続する経路です。公式説明では、
クライアントごとのタブグループが操作対象で、初回の接続ページからタブを選びます。

- 公式説明: https://github.com/microsoft/playwright/tree/main/packages/extension
- 拡張機能ID: `mmlmfjhmonkocbjadbfplnigmagldckm`
- 今回のmacOSではChrome/Edge両方の標準プロファイルルートが存在しましたが、
  Default/Profileディレクトリ内にこの拡張機能のフォルダーは見つかりませんでした。
  設定ファイルや非標準プロファイルまでは検査しておらず、インストール不存在の
  完全な証明とは扱いません。

現行実装の接続確認省略用トークンは環境変数から読み込まれ、拡張機能接続URLの
queryへ追加され、そのURLをブラウザーの起動引数へ渡します。秘密情報を環境変数・
起動引数へ置かない運用とはそのまま両立しません。Anywhere Computer独自の承認画面は
追加せず、既存拡張機能の接続設定と資格情報の扱いを分けて設計する必要があります。
トークンの取得・設定、拡張機能の追加、個人タブへの接続はこの調査では行っていません。
