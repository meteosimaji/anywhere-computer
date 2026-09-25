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
ChatGPT のクラウド側は、この Mac の `localhost` に直接接続できません。所有ドメインを
使わない非公開の開発用接続には、OpenAI の
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
を検討できます。
OpenAI Platform 側のトンネルとローカルの `tunnel-client` が必要で、この経路は
Anywhere Computer ではまだ実機検証していません。公開 Plugin の提出には安定した
公開 HTTPS エンドポイントが必要です。
トンネルは MCP 通信を運びますが、OAuth 認可サーバーを自動では公開しません。
所有者パスワードによる OAuth 接続には、認可画面へ到達できる経路も別途必要です。

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

## macOS の通常 Chat Subchat を HTTPS MCP に追加する

ログイン済み Chrome のプロファイルと利用する通常 Chat のアカウント ID を選択済みの
所有者は、既存の HTTP サーバーへ Subchat ツールを明示的に追加できます。
`--subchat-profile` は `Default` または `Profile N` ディレクトリ、
`--subchat-ledger` は操作記録を保持する専用ディレクトリの絶対パスです。
次は値を置き換えるための例です。

```sh
anywhere http-add-tools --scope subchat_capabilities --scope subchat_catalog \
  --scope subchat_list \
  --scope subchat_send --scope subchat_recover --scope subchat_status \
  --scope subchat_wait --scope subchat_message \
  --subchat-profile /absolute/path/to/Chrome/Default \
  --subchat-ledger /absolute/path/to/subchat-ledger \
  --subchat-account-id SELECTED_ACCOUNT_ID --subchat-send-consent
```

追加後に HTTP サーバーを再起動し、接続元で新しい OAuth 認可を受けてください。
既存 grant の Subchat 権限は自動拡張されません。各ツールには個別の scope が必要です。
送信前に `subchat_catalog` の `source=http` から対応するモデル選択を取得し、
その `http_selection` と利用者が選んだモデル・エフォートを指定します。
`subchat_send` と `subchat_message` は呼び出し前に一意な `request_id` を選び、
応答が途切れたら同じ ID で状態を回収してください。未確認の送信を新しい ID で
再送しないでください。`tools/list` は許可済み Subchat ツールの定義を
Chrome を起動せずに表示し、ログイン状態は検証しません。
`subchat_status` は保存済み操作を grant ごとに、`subchat_capabilities` は
設定済みの能力を、Chrome やネットワークを使わずに読みます。
送信・回収などブラウザーが必要な操作では、最初の実行時に選択アカウントを
検証します。Chrome またはログインが利用できなくても他の HTTP ツールと
上記のローカル参照は利用できます。検証失敗後は10秒間の再試行間隔を置きます。
選択したアカウントと異なるログインでは送信できません。

この経路は macOS の選択済み Chrome プロファイルから認証を読み、ブラウザー内で
送信を準備した後、生成 POST を HTTPX で行います。Chrome の起動は必要ですが、
通常タブを作らない背景タブを使用します。ログイン切れや Chrome の変更による
動作の変化はあり得ます。通常 Chat の利用条件を確認し、第三者へのアクセス販売や
モデル蒸留を目的とした大量取得には使用しないでください。

標準の戻り先は `http://127.0.0.1/oauth/callback` と `http://[::1]/oauth/callback` です。
別の戻り先を使う場合は、初期設定時に `--redirect-uri URL` を必要な数だけ指定します。
独自指定時は標準の戻り先に追加ではなく、指定した一覧を登録します。
クライアント ID と戻り先は、利用するクライアントの設定と合わせてください。

## 所有者パスワードの変更

```sh
anywhere owner-change --resource https://your-agent.example/mcp --owner owner
```

初期設定と同じ状態ディレクトリで実行してください。現在のパスワード、新しいパスワード、
新しいパスワードの確認を端末上で非表示入力します。パスワードは引数や環境変数で渡せません。
新しいパスワードは16文字以上・UTF-8で1024バイト以下とし、現在と同じ値は拒否します。

OS 資格情報ストア内の検証用データを新しい salt で更新します。更新後の読み直しで
保存を確認できた場合だけ成功を返します。保存前に失敗して旧データが残っていれば
旧パスワードが維持されたと報告します。保存結果を確認できない場合は結果不明となり、
自動再試行や旧データの書き戻しはしません。資格情報ストアへのアクセスを復旧してから
現在と新しいパスワードのどちらが有効か確認してください。

パスワードの認証・変更・削除は同じプロセスロックで直列化します。既に完了した
パスワード認証や発行済みの OAuth 接続認可を取り消す操作ではありません。
既存接続も失効させる場合は `http-revoke` を使い、再開時に `http-enable` と新規ログインを
行ってください。現在のパスワードを忘れた場合の復旧・リセット機能は未提供です。

macOS の実際の Keychain で変更後の新パスワードの検証と旧パスワードの拒否を確認し、
試験記録をローカルの履歴資料に保存しています。
この記録のパスワード変更部分は Python API 経由です。CLI の非表示入力と確認不一致は
自動テストで検証しています。

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
CLI プロセス試験は現在 POSIX 用です。
Windows/Linux の実機上の資格情報ストアとサービス管理、自動起動、公開 HTTPS との一体化、
スリープ復帰・長時間運用は別の検証項目です。

## HTTP の診断

`anywhere http-doctor` は設定済みの127.0.0.1ポートに、認証情報なしでメタデータGETを1回送ります。
`--state-dir` で対象を指定できます。設定作成、エージェント起動、プロセス停止、認証・認可の変更は
行いません。リダイレクトを追わず、3秒の全体期限、8 KiBのヘッダー、16 KiBの本文上限を設けます。

|state|意味|
|---|---|
|configuration_unavailable|設定がない、読めない、または形式が不正|
|unreachable|接続失敗または期限超過。プロセスが停止したという断定ではない|
|unexpected_response|接続先が想定した形式のHTTPメタデータを返さない|
|resource_mismatch|応答のresourceが設定と違う|
|metadata_reachable|ループバックから設定と同じresourceのメタデータを取得できた|

終了コード0はmetadata_reachableの場合だけです。それ以外はJSON診断結果と終了コード1を返します。
`authenticated: false`、`public_reachability: unverified` を常に明示します。
同じメタデータを返す別サービスとの暗号学的な識別、稼働中ビルドの一致、公開HTTPSの証明書や経路、
ユーザーのログイン・許可済みツールの動作はこの診断では検証していません。
これらは実際の接続プロファイルの `device-status` と、接続元での認可・ツール実行で別途確認します。

## 異常終了後の再起動

設定とowner-initが済んでいる場合、`anywhere http-watch` で前景監視を開始できます。
同じPython実行環境からhttp-serveを起動し、その子が異常終了したときだけ再起動します。
待ち時間は1・2・4・8・16秒で、短時間の異常終了に対する再起動は最大5回です。
子が300秒以上生存した場合は連続失敗回数をリセットします。これは応答健全性の判定ではありません。
終了コード0と130では再起動せず、監視も終了します。

同じ状態ディレクトリのwatcherは専用ロックで1つに制限します。開始時点ですでにserver lockが
使われている場合は拒否します。チェック後の競合や再起動待ちの間に手動http-serveが先に起動した場合は、
server lockが子の重複稼働を防ぎ、watcherの子は起動に失敗します。既存サービスを停止・引き継ぎせず、
再試行上限まで失敗したらwatcherは終了コード1で停止します。

Ctrl+C、またはPOSIXのSIGTERMで監視を止めると、自分が起動した子に中断を送ります。
10秒で終了しなければその子を強制終了します。Windowsは新しいプロセスグループにCTRL_BREAKを
送り、送信できない実行環境では子の強制終了へ進みます。
強制終了時は未完了操作の結果確認が必要です。監視やサーバー自体のクラッシュ後に
既存terminal sessionが復元される保証はなく、別プロセスとして残る作業もあり得ます。

これは前景コマンドです。OS起動時の自動起動は登録しません。監視自体のSIGKILL、OS再起動、
スリープ中、実行中プロセスのハング、ネットワーク断、TLS/プロキシ障害を検出・修復する機能ではありません。
秘密情報は子の引数に含めず、子は既存のOS資格情報ストアを使用します。

実プロセスの再起動回数・待機時間・正常終了・中断終了をpytestで検証します。
[scripts/verify_http_watch.py](../scripts/verify_http_watch.py) はPOSIXの使い捨て環境で
native keyringと実http-watchを使い、子の強制終了後の同一ポート復帰、重複監視拒否、
監視中断後の子終了とクリーンアップを確認します。
Windowsの実対話コンソールや公開HTTPS経路での障害復旧は、この試験の検証範囲に含みません。
