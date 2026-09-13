# 新規チャット受け入れの検証記録

## 対象と証拠の範囲

ChatGPT Webの新規チャットを開き、送信前にGPT-5.6 Solを選択し、
「5.6 中程度」の表示を確認した。新規画面の既定は「最新」だったため、
以前の会話のモデル選択を引き継いだとは仮定していない。
以前の端末ID・ツール定義・セッションIDをプロンプトに埋め込まず、
Plugin発見からMacと登録端末を確認するよう依頼した。

稼働サービスはPython版 `0.1.0a9`、runtime ID
`42f10d9ce185a8ad96cd9f90d2e5b4aeaa472ac94a4b4ce09154b47c85f03314`。
これは開発ブランチの新しい配布物と異なる。以下を最新ソースや新しい中継の
本番受け入れに流用しない。会話URL・私用項目を含む観測全文は公開記録へ含めない。

## 結果

| 対象 | 結果と根拠 |
|---|---|
| Macのファイル・検索・端末 | Chat回答で日本語・絵文字の作成、SHA-256付き追記、読戻し、検索、同一端末セッションの入出力・終了を報告。これら全件の操作記録の独立照合は未実施 |
| node_repl | Chatが同一Pluginセッションで40から42へ変更。`c0ffee00000000000000000000000016`を別の呼び出しで台帳から回収し、completed・is_error=false・結果42を確認 |
| Mac TextEdit | Chatが新規文書に入力・別呼び出しで追記・再観測。最終観測`c0ffee00000000000000000000000025`を独立回収し、completed・is_error=false、本文要素の「日本語入力: 40」「追記: 42 ✅」を確認 |
| GUI対象の限定 | 最初の観測で既存文書を捕捉したとChatが報告。その後の新規文書の操作成功で、この初期観測の問題を合格扱いにはしない |
| Windows | 登録は発見できたが、端末スキーマとstatus要求が送信前に失敗。Windowsでのファイル・GUI試験は未実施 |
| 終了確認 | 別のcomputer_status呼び出しでterminal・Plugin・direct MCP・検索・実行中操作がすべて0、update_blocked=falseを確認 |

GUIの実時間はChat報告で起動1.58秒、初回入力3.40秒、追記0.86秒。
これらは今回の全操作について独立した計時器で測定した値ではなく、
提供元が返したとChatが報告した値である。

## Windowsの失敗の追加調査

Mac側のSSH接続はバナー待ちでタイムアウト。QEMUプロセスは残っていた。
実際に稼働しているWindows for Macアプリを実行パスで特定して観測すると、
空き容量23.99 GBが24 GBの停止基準を下回ったため保護停止しており、
32 GB以上へ戻してから手動再開する表示だった。通常のスリープや
Pluginの削除と断定しない。VMの強制終了・保護機能の解除は行っていない。

新規チャットからMacのコア機能とTextEdit操作が成立した証拠は得たが、
Windows・新しいGUIアダプター・中継・ベータ版全体の受け入れは未完了。

## Windows CIの独立確認

PR #20の実行 `34790352450`（対象コミット
`9f96aeb46b6fad4ada1d6ce937d0a12f5f7f2446`）から
`pytest-results-windows-latest` 成果物の `pytest-results.xml` を取得し、
次の8件に failure・error・skipped 要素がないことを確認した。

- `test_fresh_chat_discovers_and_operates_without_previous_session`
- `test_pc_initiates_and_recovers_commit_after_lost_reply`
- `test_registry_owner_and_revocation_checked_on_live_connection`
- `test_replacement_invalidates_old_inflight_reply_without_offline_queue`
- `test_unregistered_certificate_never_becomes_an_execution_target`
- `test_wrong_reply_identity_and_timeout_close_without_replay`
- `test_two_registered_pcs_route_concurrently_by_id_not_display_name`
- `test_isolated_listener_rejects_public_bind_and_unverified_tls`

これはWindows runner上のHTTP・中継機械試験である。
新規ChatGPT会話、Windows VM、GUI、実利用者の認証、公開中継の
実操作をこの結果から合格扱いしない。
