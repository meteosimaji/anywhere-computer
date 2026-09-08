# 永続ダウンロード

大きいファイルの連続取得には `download_begin`、`download_read`、`download_status`、
`download_close` を使います。外部ライブラリを追加せず、SQLiteへ独立したコピーを保存します。

1. 新しい32桁の小文字hex `transfer_id` と元ファイルの絶対パスを `download_begin` に渡します。
   期待する全体hashがある場合は `expected_sha256` も指定します。IDは接続元で保持します。
2. 準備が完了すると `state: ready`、`total_bytes`、全体 `sha256` が返ります。
3. `download_read` に同じID、`offset`、最大262144の `limit` を指定します。
   base64データ、チャンクhash、次のoffset、EOFを受け取ります。任意の範囲から再開できます。
4. 受信側でも各チャンクと結合した全体のhashを検証し、最後に `download_close` でコピーを解放します。

元ファイルは準備時に一度だけ走査されます。その後の範囲取得では、索引で最大2つの
保存済みチャンクを読みます。元ファイルの変更・削除やサーバー再起動の後も、保存した内容を返します。
元パスが現在同じ内容であることを保証する結果ではありません。
準備中のサイズ・更新時刻・ファイル同一性の変化は拒否しますが、外部アプリの同時書き込みに対する
OSレベルの原子的スナップショットではありません。必要なら元アプリの書き込みを止めて準備します。

準備は単一DBトランザクションです。途中終了ではコピーを公開せず、成功後の応答喪失では
`download_status` でreadyを確認できます。同じID・同じ引数のbeginは元コピーの情報を返します。
unknownなら保存済みコピーは確認できていません。実行がまだ進行中の場合もあるため、
`operations_get` で元操作を照会してください。新しい内容の取得には新しいIDを使います。
closeは繰り返せます。closedのIDは再利用せず、その後の新しいreadは拒否します。
既に完了した操作IDを再送すると、エンジンの操作記録から以前の応答が返る場合があります。

1件1 GiB、各エンジンでready最大8件・合計4 GiBです。ローカル用とHTTP用のDBは別で、
アップロード用の予約制限とも別です。SQLite/WALと操作記録の容量はこのデータ上限に含まれません。
特にエンジン経由のreadはbase64応答を操作履歴にも保存します。
closeはDBのチャンクを削除しますが、空きページは再利用用に残り、物理ファイルの縮小や
安全消去、操作履歴の削除は行いません。自動期限・古い認可の管理画面は未実装です。

準備中は同じDBへの別の準備・closeが待機します。10秒を超える競合は失敗となり得ます。
HTTPの応答待ち上限より準備が長くなる場合、操作IDとtransfer IDで結果を照会してください。
別HTTP grantや別TLS peerから同じIDのコピーは参照できません。新しいgrantでは古いgrantの
コピーを再開できないため、古いgrantの有効期間内にcloseする運用が必要です。

## 検証

17 MiBの全量取得、任意境界、元ファイル削除、DB再オープン、破損拒否、容量制限、
途中プロセス終了、認可ID分離、実HTTPクライアント経由の分割取得をテストします。
`scripts/verify_download_copy.py --receipt <保存先>` は1 GiBを実際に作成し、
コピー準備後に元ファイルを削除してDBを開き直し、4096チャンク全量を取得してhashを確認します。
これはローカルDownloads APIの検証で、Engineの操作記録やインターネット全量転送の測定ではありません。

[2026-09-09の1 GiB全量測定](research/2026-09-09-download-copy-verification.json) は、
コピー準備3.50秒、4096チャンク取得4.60秒、全体hash一致、closeと一時ディレクトリ削除を記録しています。
この値は当該MacのローカルAPIの測定であり、ネットワークの転送速度ではありません。

Windows互換性では、Python 3.12のパスstatとfd fstatでctimeの意味が異なるため、
ctimeはfdの前後だけで比較します。パスとfdの同一性・サイズ・mtimeの比較は維持します。
参照: [CPythonのパスstat処理](https://github.com/python/cpython/blob/3.12/Modules/posixmodule.c)、
[fd情報の取得処理](https://github.com/python/cpython/blob/3.12/Python/fileutils.c)。

旧認可の保存データは [所有者のローカル転送管理](TRANSFER-MANAGEMENT.md) で一覧・解放できます。
