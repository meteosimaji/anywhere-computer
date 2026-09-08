# HTTP サーバーの設定と起動

現在は、一つの所有者・端末・登録クライアントを持つ HTTP サーバーを起動できます。
コードは Windows・macOS・Linux で共通です。ローカルの `mcp` エージェントとは別の
状態ディレクトリを使い、HTTP の操作記録とバックアップを保持します。
同じルート保存先のローカルエージェントとはファイル書き込みロックを共有します。
これにより両方からの書き込みを調停しつつ、操作記録を別々に保持します。
別の保存先で起動したエンジンや外部アプリの変更までロックするものではありません。

## 初期設定

先に、利用する固定 HTTPS URL を決めます。以下のホスト名は説明用です。
サーバーの作成だけでは外部から到達可能になりません。

```sh
anywhere http-configure --resource https://your-agent.example/mcp --owner owner --client-id anywhere-native --scope files_read --scope files_write --scope operations_get --port 8768
anywhere owner-init --resource https://your-agent.example/mcp --owner owner
anywhere http-serve
```

`http-configure` は公開設定と認可データベースを作成します。`owner-init` は非表示の
パスワード入力で所有者認証を設定します。`http-serve` は両方の整合を確認してから
フォアグラウンドで待ち受けます。いずれも同じ状態ディレクトリを使ってください。
必要ならすべてのコマンドに同じ `--state-dir` を指定します。

設定を確認するには `anywhere http-show` を実行します。表示は保存済みの設定であり、
稼働・公開到達性の診断ではありません。既存設定の上書きや初期化は行いません。
設定変更・追加クライアント登録・再承認の管理画面は今後の作業です。

標準の戻り先は `http://127.0.0.1/oauth/callback` と `http://[::1]/oauth/callback` です。
別の戻り先を使う場合は、初期設定時に `--redirect-uri URL` を必要な数だけ指定します。
独自指定時は標準の戻り先に追加ではなく、指定した一覧を登録します。
クライアント ID と戻り先は、利用するクライアントの設定と合わせてください。

## HTTPS の接続経路

HTTP サーバーは **127.0.0.1 のみ**に待ち受けます。固定 HTTPS の接続口は、同じホスト上の
reverse proxy 等からこの待受へ転送します。必要な契約は次のとおりです。

- HTTPS URL の origin を設定した `resource` と一致させる。
- `/mcp`、`/authorize`、`/oauth/token` と `/.well-known/` の各経路を転送する。
- バックエンドへの `Host` を `127.0.0.1:8768` に書き換える（設定したポートを使う）。
- ブラウザーの `Origin`、`Cookie`、`Authorization`、MCP のセッション・プロトコル
  ヘッダーを保持する。認可 POST の Origin は公開 HTTPS origin の完全一致が必要。
- 応答、認可フォーム、コード、トークンをキャッシュしない。認可情報を含む URL、
  リクエスト本文、ヘッダーをアクセスログに記録しない。
- トークン交換やツール実行の POST を、通信障害時に自動再送しない。

起動時にはローカル待受と `public_reachability: unverified` を表示します。これは固定 URL の
証明書・公開到達性・proxy 設定を確認したという意味ではありません。現段階では
TLS 証明書の取得・更新、常設 NAT 越えの経路、公開側の負荷制限を自動構築しません。
Quick Tunnel の試験成功も常設サービスの運用保証ではありません。

## 接続と失効

接続元でブラウザー認可を行い、同じプロフィールで MCP を起動します。

```sh
anywhere login --resource https://your-agent.example/mcp --client-id anywhere-native --profile laptop --scope files_read --scope files_write --scope operations_get
anywhere http-mcp --resource https://your-agent.example/mcp --client-id anywhere-native --profile laptop
```

サーバー側で `anywhere http-revoke` を実行すると、現在の端末の全 grant が失効します。
サーバーが起動中でも実行でき、再起動後も失効を維持します。すでに開始した操作を
巻き戻す機能ではありません。
パスワード検証用データの削除だけでは、発行済み grant は失効しません。

```sh
anywhere http-auth-status
anywhere http-revoke
anywhere http-enable
```

`http-auth-status` は保存された端末 ID と `device_enabled` を表示します。
この値は新しい認可を受け付ける状態を示し、サーバーの稼働、公開 URL の到達性、
クライアントのログイン成功を示しません。

`http-enable` は失効させた端末を再有効化します。端末 ID・クライアント・戻り先・
許可する操作は保持し、過去の grant を失効済みに固定してから新しい認可を受け付けます。
接続元で `login` をやり直してください。古い access token、refresh token、未交換コード、
失効前に開いた認可画面は再利用できません。認可画面の所有者パスワードを検証している
途中で失効と再有効化が起きても、古い要求からのコード発行は拒否します。
旧 MCP セッションも再利用できず、新しい認可で接続を作り直します。

変更は SQLite の一つのトランザクションで行います。失敗した更新を半分だけ保存しません。
実行中の同じバージョンのサーバーにも反映します。ソフトウェアを更新した場合は、
サーバーを新しいバージョンで再起動してから管理コマンドを使ってください。
既に有効な端末で `http-enable` を繰り返すと `changed: false` を返し、新しく承認した
接続を失効させません。存在しない端末を新規登録するコマンドではありません。

`http-serve` は Ctrl+C で終了します。新しい HTTP 接続を停止し、受理済み処理の終了を待ち、
当サービスの端末セッションとデータベースを閉じます。プロセス終了・クラッシュをまたぐ
対話プロセスの維持は未対応です。HTTP 接続だけの切断ではエンジンは終了しません。

## 保存と復旧の境界

設定と初期の認可データは一時ディレクトリで組み立て、まとめて公開します。失敗した設定を
起動可能な状態として残さず、既存設定を上書きしません。同じ状態ディレクトリでは、OS が
解放するプロセスロックにより二重起動を拒否します。ポートが使用中なら起動を失敗として
報告し、ロックを解放します。別プロセスを停止してポートを奪うことはありません。

起動時には設定と認可 DB の端末、所有者、クライアント、戻り先、機能一覧を照合します。
DB が消失・不一致の場合は起動を拒否し、失効済みの権限を作り直しません。
破損・消失した認可データの復元手順、設定の移行・更新と rollback は今後の作業です。

操作記録は `http-server/engine` に残ります。同じ grant と同じ操作 ID・引数による要求は、
再起動後も記録済みの結果を返し、書き込みを繰り返しません。クラッシュ時に実行中だった
操作は `unknown` となり、自動的な再実行はしません。OS 全体の電源断やディスク故障に対する
完全な保存保証を検証したものではありません。

ローカル HTTP 試験では認可フォームからコード交換、書き込み、再起動後の重複抑制、
実行中の失効、再有効化後の古い接続の拒否と新規ログイン、再起動後の状態保持を確認します。
認可 DB の旧バージョンからの移行と、失効前の認可画面の拒否も回帰テストに含みます。
CLI プロセス試験は現在 POSIX 用で、macOS での
実行記録を [保存しています](research/2026-09-09-http-service-verification.json)。
再有効化コマンド・繰り返し実行・再起動後の状態確認の
[実行記録](research/2026-09-09-http-service-reauthorization-verification.json)も保存しています。
Windows/Linux の実機上の資格情報ストアとサービス管理、自動起動、公開 HTTPS との一体化、
スリープ復帰・長時間運用は別の検証項目です。
