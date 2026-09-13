# Remote Desktop Commanderの公開コード確認

確認日: 2026-09-12。ソースの読取結果であり、ホスト型サービスの実操作試験ではない。
依存のインストール、端末のペアリング、利用者アカウントへの接続は行っていない。

## 固定した対象

- [端末側ソース](https://github.com/wonderwhy-er/DesktopCommanderMCP/tree/a781f5a4b8cfebac6638bc6fcbd38fca6326be53): package.json は0.2.50、MIT。
- [Remote側公開物](https://github.com/desktop-commander/remote-desktop-commander/tree/b480501dcca59f802ebaf97f2f57b45252d0b720): 文書とマニフェスト。ホスト型サービス本体の実装は含まれない。

## 確認した実装

`src/server.ts` の一覧登録と呼出し分岐には、ファイル読書き・複数ファイル読取・PDF書込・
ディレクトリ作成と列挙・移動・部分編集、検索開始と追加取得・停止、プロセス開始・出力取得・
入力・終了・セッション列挙がある。登録があることは、各機能の実機成功の証明ではない。

`src/remote-device/desktop-commander-integration.ts` はMCP SDKのstdioクライアントで
同梱のローカルDesktop Commanderを起動し、tools/list と tools/call を中継する。
この接続処理自体にはモデル生成呼出しがない。任意のCodex MCP登録を読み込む実装ではなく、
自分自身のdist/index.js、次いでグローバルのdesktop-commanderを探す。
子プロセス切断でreadyを解除し、次の要求で初期化する。同時の再初期化はPromiseを共有する。

`remote-channel.ts` のmarkCallExecutingはDB行をpendingからexecutingへ条件付き更新する。
DBエラー時はtrueを返して実行を続けるため、この関数だけからプロセスをまたぐ厳密な
一度限りの実行を保証できない。device.tsには別にメモリ上のseenCallIdsがある。
これはコード上の限定であり、公開サービスで重複実行を再現したという報告ではない。

結果はupdateCallResultで保存する。NULを除去し、結果保存に失敗すると文字列だけの失敗結果を
保存し直す処理がある。ツール実行と結果の配達・保存を分けて扱う設計は参考になる。

## Anywhere Computerへの反映方針

- 操作用のモデルを別途起動せず、既存MCPの実行と結果の中継を中心にする。
- 接続の生存と、実際のローカルMCPの生存を区別する。
- 長い処理の出力は必要な部分を後から取得する。
- 応答喪失時は操作台帳を参照する。状態を保持するMCPセッションを黙って作り直さない。
- 第三者のサービス実装をコピーしたとは扱わない。現時点ではコードの取り込みはしていない。

GUI操作、ブラウザーの既存ログイン利用、全ツールの実操作、RemoteのOAuth・復帰の実機検証は
この確認の範囲外。Computer Use認証拒否の解決をこの調査で達成したとは扱わない。

## 2026-09-13: ファイル・検索・端末の実用確認

上記2リポジトリのHEADを再照会し、固定点と一致した。端末側package.jsonは0.2.50。
Remote公開リポジトリは文書・マニフェストであり、ホスト型サービス本体をコード監査した
とは扱わない。今回はユーザー指定によりファイル・検索・端末を優先した。

Anywhere Computerの実ファイル・実プロセスを使う試験
`tests/test_file_search_terminal_workflow.py` を追加した。モデルやCodexのプロセス代替fixtureを
使わず、Engineの公開ツール契約を通して以下を検証する。

- 日本語と絵文字を含むファイルを作成し、検索結果を1件ずつcursorで回収。2件の欠落・重複なし。
- 複数ファイル読取では正常な内容と存在しないファイルの個別エラーを同時に取得。
- 読取時のSHA-256を条件に対象箇所だけ編集し、別の40は維持。
- 同一の実端末セッションに2回入力し、ファイル変更の42→50と日本語・絵文字を確認。
- 明示したプロンプトまで待機し、出力欠落なし、最後にプロセス終了を確認。

この新規試験は1 passed。検索・端末出力・端末対話・HTTP能力試験の関連4ファイルは
31 passed。新規試験のRuffとgit diff --checkも成功。これはmacOS上の結果であり、
ChatGPT側の安全性判定、実インターネット経路、他OSの実行成功を証明しない。
HTTP能力試験のCodex部分は合成peerであり、全外部Pluginの実動作確認とは区別する。

### 次の改善候補

端末側`src/utils/process-detection.ts`を読むと、Desktop Commanderは既知プロンプトや
出力中の終了・エラー文字列から状態を推定する。Anywhere Computerは実プロセスの終了状態と
利用者指定のliteralプロンプトを使い、自動推定は実装していない。
比較した`src/server.ts`のstart_process契約は開始時の出力待機も提供する一方、
Anywhereのterminal_startは開始情報だけを返し、出力には次の呼出しが必要となる。

まず検討すべき小さな改善は、terminal_startに任意の短い出力待機を追加し、開始と最初の
出力取得を1回で済ませること。既定の即時返却と既存cursor契約を保ち、既存のwait_outputを
再利用する。自動プロンプト判定・PTY・PDF処理を一度に追加する計画ではない。
この候補は本確認では未実装。既存の機能充足監査の残不足を解消済みには数えない。

### alpha8修正時の追加照合

2026-09-13にDesktopCommanderMCPのHEADを再照会し、a781f5a4と一致した。
src/terminal-manager.tsのexitハンドラーは親プロセスの終了時に完了履歴へ移して
アクティブセッションから削除する。forceTerminateは登録されたprocessへSIGINTを送り、
一秒後にも登録が残ればSIGKILLを送る。この実装の読取だけで孤児化した子の追跡を
保証できないため、Anywhere側の修正にはそのまま採用していない。
相手製品で同じ障害を実行再現したという報告ではない。

Anywhere alpha8では、POSIXグループ/Windows Jobの所有元を保持する方式を採った。
UI表示やプロンプト文字列から実プロセス群の終了を推測しない。要約・検索・個別schema取得は
直接MCPへ追加し、GUIについては別途明示したPeekabooアダプターを用意した。
