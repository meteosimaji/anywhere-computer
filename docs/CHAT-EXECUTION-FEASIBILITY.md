# Chatからの操作経路の確認

2026-09-12。目標はChatが判断し、Anywhere Computerがホスト上で操作を実行して
観測結果を返すこと。Codexのモデル生成や内部Computer Useの文脈を必須にしない。
操作許可はChat側に任せ、独自の操作承認画面は追加しない。

## 直接呼び出しで確認した範囲

インストール済みローカル経路とChat接続先を、このCodexから直接ツール呼び出しした。
別のChatGPT会話への依頼・生成は行っていない。

- 両経路で専用一時ファイルを作成、読み取り、ハッシュ付き追記、再読み取り。
  日本語と絵文字を含む保存内容・SHA-256一致を確認。
- 両経路で端末を起動し、別の呼び出しから入力して出力を回収。終了コード0。
- 書き込みの操作IDを使った台帳回収が元の応答と一致。
- Chat接続先のPluginセッションでnode_replの変数40を次の呼び出しで42へ更新。
  終了処理のcleanup_confirmed=trueを確認。
- 同じChat接続先のterminal_startから、開発環境のPlaywright 1.58.0を実行。
  専用Chromium 145.0.7632.6で無害なページに入力、クリック、DOM結果の読み取り、
  スクリーンショット保存を実施。結果「受信: Anywhere Computer」、終了コード0。
  files_read_binaryからPNGも回収できた。外部サイトへの送信は行っていない。

以上は独立したブラウザー操作をPlugin経由で実現できる実証である。
まだ専用browserツールとして組み込まれた機能、通常ブラウザーの既存タブへの接続、
配布物だけで動くセットアップ、ネイティブGUIの操作成功を意味しない。

## 残る条件

実際のPlugin子プロセスでAXIsProcessTrustedとCGPreflightScreenCaptureAccessを
読み取った結果はどちらもfalse。権限要求のダイアログはこの試験では出していない。
macOSネイティブ操作には、安定した実行主体とOSのアクセシビリティ・画面収録の
初期許可が必要。独自の操作承認ではなくセットアップとして扱う。

Codex内部のcua_replを中継するだけでは、正式な操作文脈が得られるとは確認できない。
独立アダプターを実装し、観測・対象指定・操作・再観測をPluginの契約として提供する。
Codexアプリ固有UI、他Pluginのウィジェット、ホスト独自機能の完全互換は別途検証が必要。

実操作記録はoutput/plugin-direct-live-20260912.jsonと
output/browser-plugin-feasibility-20260912.json。outputはローカル検証用で配布対象外。
GitHubで確認できる共有用抜粋はresearch/2026-09-12-checkpoint-live.json。
