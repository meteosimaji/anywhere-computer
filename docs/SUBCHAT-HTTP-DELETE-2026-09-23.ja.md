# 通常 Chat の HTTP 削除

`subchat_delete` は、保存済み subchat の会話を通常 Chat の認証済み HTTP 経路で
非表示にする操作です。MCP の引数と JSON-lines CLI の `delete` action は
`operation_id` と `conversation_id` のみです。指定した operation に保存された
会話 ID と一致しない場合はリクエストを送りません。アカウント ID は台帳と現在の
認証済みセッションの間で照合し、ツール引数や結果には含めません。

```json
{"action":"delete","operation_id":"<保存済みの32桁ID>","conversation_id":"<保存済みのUUID>"}
```

削除前に同じ認証セッションで対象会話を GET し、会話 ID、保存済み入力 ID、本文、
リソースを照合します。SQLite に一回限りの送信権を記録した後、
`PATCH /backend-api/conversation/{id}` に `{"is_visible":false}` を送ります。
HTTP 200 と JSON `success: true` に加え、その直後の同じ会話履歴 GET が 404
だった場合だけ `deleted` を返します。PATCH 後の通信失敗や予期しない応答は
`unknown` として保存し、自動再送しません。`deleted` はこの非表示操作の確認であり、
サーバーからの物理的な消去を意味しません。

2026-09-23 に、この作業のためだけに作られた会話で実機確認しました。削除前の
認証済み GET は保存済み会話・入力・本文と一致し、PATCH は HTTP 200 の
JSON `{"success":true}`、直後の同一会話 GET は HTTP 404 でした。既存の
ユーザー会話には削除を行っていません。秘密ヘッダー値は記録していません。

CLI でブラウザーを使う場合は、既存の専用 `--browser-profile` と元の
`--state-dir` を指定します。ブラウザーなしの場合は `--http-only`、元の
`--state-dir`、認証済みの `--http-session-stdin` 引き渡しを使います。
認証情報の取得・更新や HTTP 生成は、この削除機能には含まれません。
