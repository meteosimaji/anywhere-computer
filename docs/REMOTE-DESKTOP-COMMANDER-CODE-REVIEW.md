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
