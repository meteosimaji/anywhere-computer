# Desktop Commander 公開ソース案内

調査日: 2026-09-08。実行エンジンと端末側 Remote エージェントは公開ソースで確認できる。クラウド中継サービスそのものは非公開。

## 閲覧先

|役割|公式ソース|確認できるもの|
|---|---|---|
|ローカル engine|[DesktopCommanderMCP](https://github.com/wonderwhy-er/DesktopCommanderMCP/tree/56deabc3fe3c586f91728c56da5715712ff34eb6)|MIT、ファイル・検索・文書・端末・UI・Remote 端末コード|
|Remote 製品資料|[remote-desktop-commander](https://github.com/desktop-commander/remote-desktop-commander/tree/b480501dcca59f802ebaf97f2f57b45252d0b720)|公開接続情報・手順・manifests。サーバー実装ではない|

## 読む順序と呼び出し経路

固定 commit のファイルリンクで確認する。

1. [package.json](https://github.com/wonderwhy-er/DesktopCommanderMCP/blob/56deabc3fe3c586f91728c56da5715712ff34eb6/package.json): v0.2.48、依存ライブラリ、build/test コマンド。
2. [src/index.ts](https://github.com/wonderwhy-er/DesktopCommanderMCP/blob/56deabc3fe3c586f91728c56da5715712ff34eb6/src/index.ts): CLI の入口。
3. [src/npm-scripts/remote.ts](https://github.com/wonderwhy-er/DesktopCommanderMCP/blob/56deabc3fe3c586f91728c56da5715712ff34eb6/src/npm-scripts/remote.ts): remote 起動、セッション再利用、logout、sleep 抑制オプション。
4. [src/remote-device/device.ts](https://github.com/wonderwhy-er/DesktopCommanderMCP/blob/56deabc3fe3c586f91728c56da5715712ff34eb6/src/remote-device/device.ts): 起動/認証/登録/dispatch/停止。
5. [device-authenticator.ts](https://github.com/wonderwhy-er/DesktopCommanderMCP/blob/56deabc3fe3c586f91728c56da5715712ff34eb6/src/remote-device/device-authenticator.ts): device code、PKCE、承認待ち polling。
6. [remote-channel.ts](https://github.com/wonderwhy-er/DesktopCommanderMCP/blob/56deabc3fe3c586f91728c56da5715712ff34eb6/src/remote-device/remote-channel.ts): Supabase Auth/Realtime、端末状態、要求行の取得、結果返却、接続回復。
7. [desktop-commander-integration.ts](https://github.com/wonderwhy-er/DesktopCommanderMCP/blob/56deabc3fe3c586f91728c56da5715712ff34eb6/src/remote-device/desktop-commander-integration.ts): 子 MCP への stdio 接続、tools/list と tools/call の中継、子プロセスの再初期化。
8. [src/server.ts](https://github.com/wonderwhy-er/DesktopCommanderMCP/blob/56deabc3fe3c586f91728c56da5715712ff34eb6/src/server.ts): ツール一覧登録、schema 接続、switch dispatch、UI resources。
9. [src/tools/schemas.ts](https://github.com/wonderwhy-er/DesktopCommanderMCP/blob/56deabc3fe3c586f91728c56da5715712ff34eb6/src/tools/schemas.ts): 引数型と検証。
10. [src/handlers](https://github.com/wonderwhy-er/DesktopCommanderMCP/tree/56deabc3fe3c586f91728c56da5715712ff34eb6/src/handlers) → [src/tools](https://github.com/wonderwhy-er/DesktopCommanderMCP/tree/56deabc3fe3c586f91728c56da5715712ff34eb6/src/tools) → [形式別 handler](https://github.com/wonderwhy-er/DesktopCommanderMCP/tree/56deabc3fe3c586f91728c56da5715712ff34eb6/src/utils/files): 実際の操作。
11. [src/ui](https://github.com/wonderwhy-er/DesktopCommanderMCP/tree/56deabc3fe3c586f91728c56da5715712ff34eb6/src/ui): ファイルプレビュー、設定エディター、ツールを呼ぶ UI。

## 公開クライアントから確認できた Remote の通信

以下は固定 commit のコードを読んだ結果。提供中サーバーへ要求を送って観測したものではない。

|段階|コードに存在する通信/状態|代替実装への意味|
|---|---|---|
|設定|base server URL から `/api/mcp-info` を GET|現行端末は Supabase 接続情報を取得する|
|端末認証開始|`POST /device/start`、device name/type/ID、PKCE challenge|ブラウザ承認に紐づく登録処理が必要|
|認証待ち|`POST /device/poll`、device code と verifier、pending/slow_down/期限切れ|成功状態と待機状態を明確に分ける|
|端末登録|`mcp_devices` と capability、device name/ID|端末の発見と利用可能ツールの紐づけ|
|イベント|private な `user:<user-id>` channel、端末 ID の presence|端末所有者に限定した配送と到達性|
|要求到着|`new_call` の通知後、`mcp_remote_calls` から要求行を取得|通知と payload 本体を分離した構造|
|実行|pending → executing の claim、端末内の呼出し ID 重複抑制|再配送で同じ副作用を起こさない設計|
|ローカル呼出し|stdio MCP の `callTool`、remote metadata|既存 engine を adapter 越しに再利用できる|
|結果|要求行へ status/result/error/completed_at を更新し `result` イベント|実行完了と通知完了を区別する|
|回復|heartbeat、token refresh、presence 復旧、子 MCP の再初期化|有料サービス相当の安定性を作る主要部分|

現行コードでは `MCP_SERVER_URL` で base URL を変更できる。ただし URL を替えるだけでは動かず、その先に device auth、Supabase と期待される schema/channel/権限制御等が必要。自作は外側の標準 MCP 契約と利用体験を保ち、内部の接続方式は独立設計にできる。

一部コメントにはサーバー側 polling や exactly-once の表現があるが、コメントだけをサーバー実装や障害時保証の証拠にしない。例えば claim の DB 更新失敗で実行を継続する経路があるため、再起動を跨ぐ副作用の保証は独自の永続状態と失敗試験で定義する。

## 非公開部分と追加調査の境界

クラウド側の OAuth client 実装、API/DB schema 全体、RLS policy、quota/billing、dashboard、routing の実装は、この公開 repo だけでは確定しない。

互換機能の要件定義には、公開コードと標準 MCP から十分な出発点が得られた。追加で必要なら、自分のアカウントの通常操作を対象に、認可された接続の tools/list・応答 schema・動作を比較するブラックボックス試験を行える。今回はその試験、非公開コードの取得、サービスへの挙動変更、料金制限を回避する操作を行っていない。

Remote 公開資料・ブランドのライセンスは MIT ではないため、リポジトリには公式リンクと独自の分析を収録する。今後ローカル MIT コードを取り込む際は第三者著作権表示を保持する。
