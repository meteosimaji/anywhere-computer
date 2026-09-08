# 所有者によるローカル転送管理

`transfers` と `transfer-release` は端末所有者がローカルCLIから使う管理コマンドです。
MCPカタログには登録せず、リモート接続の全履歴一覧としては公開しません。
新しいHTTP認可から参照できなくなった、以前の認可の転送データも管理できます。

```sh
anywhere transfers --transfer-area http --transfer-kind download
anywhere transfers --transfer-area local --transfer-kind upload --limit 20
```

`--transfer-area` と `--transfer-kind` は必須です。HTTP用のデータは
`http-server/engine`、ローカル用は状態ディレクトリ直下のエンジンを参照します。
別の状態ディレクトリは `--state-dir` で明示します。状態DBがない場合は作成せず、
`registry_exists: false` と空の一覧を返します。エージェント起動や認証操作は行いません。

結果には `storage_id`、元または保存先のパス、状態、サイズ、hashが含まれます。
アップロードの結果不明状態は `unknown` と表示し、一時ファイルの位置も確認できます。
`storage_id` はDB内部の識別子です。リモート呼び出しで指定したtransfer IDとは異なる場合があり、
この値をリモートMCPのtransfer IDとして渡しても同じ記録にはアクセスできません。
元のgrantやアカウントの逆引きはできません。パス・サイズ・状態を確認して対象を選びます。

1ページ最大100件です。`next_after` が返ったら次の呼び出しの `--after` に指定します。
一覧はclosed/abortedなどの記録も含みます。ページ間の一貫したスナップショットではなく、
並行して新規作成された記録は再度先頭から一覧しないと見つからない場合があります。

```sh
anywhere transfer-release --transfer-area http --transfer-kind download --storage-id <一覧のID>
anywhere transfer-release --transfer-area local --transfer-kind upload --storage-id <一覧のID>
```

downloadはコピーのチャンクを削除してclosedへ、uploadは受信中のチャンクを削除してabortedへ
遷移します。繰り返し可能ですが、進行中の転送を止める操作なので、対象を確認して実行します。
アップロードの公開済み・公開結果不明の状態は、このコマンドでは解放しません。
元ファイル・保存先ファイル・残ったファイルシステム上の一時ファイルは削除しません。

論理的な予約容量は解放されますが、SQLiteの空きページ・操作履歴は残ります。
DBファイルの縮小、安全消去、自動期限、管理画面は未実装です。
CLIは端末のファイルアクセス権に依存し、別の管理者パスワードは要求しません。
状態ディレクトリへのアクセスを許可されたローカルプロセスに対する隔離機構ではありません。
