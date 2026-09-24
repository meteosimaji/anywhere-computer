# 会話からの端末指定

ローカルの `anywhere mcp` はエンジンのツールに加え、端末指定用の次の3ツールを公開する。

1. `devices_list {}` で登録済み端末を確認する。`local` はこのコネクターのコンピューター。
   保存された状態は過去の観測であり、この一覧取得では接続しない。
2. `devices_tools {"device_id":"…"}` で指定端末へ接続し、現在許可されているツールとschemaを取得する。
3. `devices_call {"device_id":"…","tool":"computer_status","arguments":{}}` で実行する。
   応答の `data.device_id` が宛先、`data.result` が対象ツールの結果。外側のoperation_id/stateを保持する。

通常の `files_read` などのツールは引き続きローカルを対象とする。暗黙の選択変更はない。
端末名ではなく登録時に発行した固定IDを使う。登録解除・再登録では新しいIDになる。
SSH端末は既存のOpenSSH aliasと厳格なホスト鍵検査、HTTP端末は保存済みprofileとnative vaultを使う。
登録・ブラウザ承認は既存のCLIで行い、会話中の呼出しが無断で認証画面を開くことはない。

alpha9ではローカルstdioに加え、HTTP入口もこの3ツールを公開する。HTTPでは対応する
明示的なtool grantが必要。新規設定のallには含まれるが、read-only/filesには追加しない。
既存の全件接続は`http-add-tools --scope devices_list --scope devices_tools --scope devices_call`
で更新し、クライアントのカタログも更新する。制限付き・失効した認可は拡張しない。
共通エンジン構成ではshared_agent_directoryの登録を共有し、それ以外ではHTTPのstate-dirを使う。
`devices_` / `connection_setup_` / `__`で始まる内部・転送用ツールは転送先一覧から除外し、実行も拒否する。
汎用のdevices_callは書込み・コード実行を含み得るため、破壊的・外部操作を伴うツールとして宣言する。

## 応答喪失と再試行

コネクターは操作IDと端末ID・接続先・ツール・引数のdigestをSQLiteに保存し、同じIDの宛先や
要求内容の変更を拒否する。名前変更は接続先を変更しない。引数本文・資格情報・応答はここには保存しない。
転送前にSQLiteで送信権を一度だけ取得し、同じIDの要求は再転送せず照会を案内する。
これにより、再ログインで対象側のgrant namespaceが変わっても古い要求を再実行しない。
転送済みマークと実送信の間に停止した場合も、送信済みとは断定できずunknownとして照会する。
応答本文のローカルキャッシュは作らない。対象側にも永続operation ledgerがある。
SSH/HTTPはoperationId capabilityを持つ相手だけを受け入れ、操作IDをそのまま渡す。
コネクター独自の結果キャッシュを認可の代わりにせず、呼出しのたびに対象の現在のcatalogと認可を確認する。

応答が不明なら `unknown` と宛先IDを返す。新しいIDで書込みを再送しない。
同じ端末に `operations_get` を呼び、その引数 `operation_id` に不明になったIDを指定する。
照会自体のIDは新しくする。接続解除の失敗で確認済みの実行結果を不明へ書き換えない。
端末ごとのsession/search/transfer/operation IDは必ずdevice_idと組にして扱う。

HTTP入口では外側の認可ごとに転送操作IDを分離する。転送した操作の照会は同じ認可から
`devices_call`のtoolに`operations_get`を指定し、元のdevice_idと返されたoperation_idを渡す。
通常のローカル操作は従来どおりトップレベルのoperations_getで照会する。
新しく認可を作り直した接続では、旧認可の転送操作を照会できない。
保存済みの対象側認証は端末の所有者のもの。tool grantsはファイルやプロセスを複数ユーザー間で
隔離する仕組みではなく、devices_callは対象側で許可された操作全体へのアクセスを含む。

## 検証と限界

Windows VMも別の操作先として扱う。ゲスト内にAnywhere Computerを配置し、ゲストの
SSHまたは認証付きHTTP接続を登録する。Macの`local`をWindowsと見なすことはできない。
接続後は対象の`computer_status`でOS・版・instanceを確認し、ファイルや端末セッションの
IDをその`device_id`と組にして保持する。VMの起動・仮想化基盤の設定はこのルーターの役割に含めない。

HTTP機械試験では実際のHTTPサーバーを2段に接続し、端末切替・日本語ファイル・応答喪失後の
照会・連続端末入力・認可分離を確認する。対象エンジンも試験ホスト上で動くため、
この試験だけで実際のWindows VMへの到達やGUI操作が成功したとは扱わない。

独立したローカル/対象エンジンの試験、実際の子プロセスを介したSSH用MCPプロトコル試験、
HTTP認証サーバーによるcatalog制限・拒否・セッション解除試験を用いる。
SSHプロトコル試験はOpenSSH自体や二台の物理端末を実証するものではない。
同一IDの再転送拒否、SSH対象側のledger復用、別端末へのID再使用拒否、接続先変更、再起動後のbinding、
応答喪失後の照会、初期化キャンセル時の子プロセス回収も検証する。

CLIの `remote-mcp` / `http-mcp` は引き続き単一端末接続として使える。
Web dashboard、照合コードpairing、遠隔agent停止、二台の実機・各OSの認証操作検証は残る。

## Selective catalogs (development)

`devices_tools` accepts optional `name`, `query` and `summary` fields. Use
`{"device_id":"local","summary":true}` for names/descriptions without schemas,
then `{"device_id":"local","name":"files_read"}` for one exact schema.
`query` searches tool names and full descriptions case-insensitively. Combined
filters intersect; no matches returns an empty list. Without these fields the
existing complete-catalog response is preserved.

This reduces the result delivered to the AI. The connector still fetches the
current authorized upstream catalog on every request, including execution. This
is not an upstream bandwidth optimization, schema cache or latency guarantee.
Changing authorization or removing a tool is reflected on the next request.
No prior schema response grants permission for a later operation.

The caller can request an exact schema directly when it already knows the name.
