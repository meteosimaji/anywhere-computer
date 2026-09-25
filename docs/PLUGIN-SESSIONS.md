# 状態を保持するプラグインセッション

Anywhere Computer が所有する Codex App Server と一時 thread を、複数の MCP
ツール呼び出しで再利用する。ChatGPT 側のモデルが判断し、Codex モデルの
`turn/start`、`turn/steer`、既存会話の `thread/resume` は送信しない。

従来の `codex_plugin_tools` / `codex_plugin_call` は、`session_id` を省略すれば
引き続き一回限りの実行環境を使う。状態を保持する動作には明示的な開始が必要である。
Subchat の送信・メッセージ・回収・待機・キュー監視はこの例外で、`session_id` がない
`codex_plugin_call` は送信前に `plugin_session_required` を返す。
ワークスペースが同じだからという理由で、別の呼び出しや認可接続へ状態を共有しない。

## 利用手順

1. `codex_plugin_session_open(cwd, idle_timeout=300)` を呼び、応答の
   `data.session_id` を保存する。cwd は既存ディレクトリの絶対パス。
2. `codex_plugin_tools(cwd, session_id, server, tool)` で必要なツールを検査する。
   返された `call_arguments` には同じセッション ID も含まれる。
3. `call_arguments` に子ツールの `arguments` を加えて `codex_plugin_call` を呼ぶ。
   次の操作にも同じセッションを渡す。各実行の直前にツール定義の指紋を再検査する。
4. `codex_plugin_session_status(session_id)` で状態を確認し、作業後は
   `codex_plugin_session_close(session_id)` で終了する。

開始時にインストール済み MCP サーバーが起動することがある。開始やツール一覧取得が
単なるファイル読取りだけで済むとは扱わない。個々の送信・購入・削除等には、その操作への
ユーザーの依頼・承認が別途必要である。子サーバーからの対話的な承認要求は自動承認しない。

## ID の区別

| 値 | 用途と寿命 |
| --- | --- |
| ツール応答の `operation_id` | 一回の操作の結果を同じ認可接続の `operations_get` で回収する。内部 SQLite ID とは異なる場合がある。 |
| プラグインの `session_id` | 今回追加した実行環境の識別子。開始時にサーバーが発行する。端末セッションの ID ではない。 |
| HTTP の `MCP-Session-Id` | MCP SDK が管理する通信セッション。これを終了・再作成しても、プラグインセッションは自動終了しない。 |
| Codex の thread ID | 内部の一時実行コンテキスト。利用者向けのセッション ID として公開せず、既存会話の再開にも使わない。 |

プラグインの所有者は認証済みトランスポートから実行タスクへ渡す。モデルが tool arguments に
`owner` や `peer` を追加して指定することはできない。HTTP では OAuth grant ごとに分離する。
同じ grant のアクセストークン更新や HTTP 再接続では継続できるが、同一人物・同一端末でも
新しい別 grant から既存セッションを操作することはできない。

この分離はプラグインセッションへのアクセス制御であり、同じ OS 所有者に与えた端末・
ファイル権限をサンドボックス化するものではない。

## 寿命と上限

一つの Engine で同時に保持できる実行環境は最大4件。作成中や後処理未確認の環境も
上限に含める。新しい環境を起動する前に枠を確保し、並行した開始でも上限を超えない。

無操作時間は既定300秒、指定可能範囲は30〜1800秒。5秒間隔の後処理に加え、
次の操作の直前にも期限を検査する。検査・実行が終了した時点から無操作時間を数え直す。
状態確認だけでは期限を延ばさない。処理中のセッションは期限切れ終了の対象にしない。
Subchat の背景送信やキュー監視を開始したセッションは、無操作期限に達したとき
`subchat_activity` で実行中か確認する。実行中、または確認できないときは終了を
保留し、活動がなくなってから通常の期限切れ処理に戻す。明示終了と Engine 終了は
この保留の対象外である。
古い Subchat サーバーに `subchat_activity` がない場合は状態変更前に
`plugin_activity_unavailable` で拒否する。監視中に活動確認が失敗した場合は
安全側に保留し、`codex_plugin_session_status` の
`background_activity_probe_failures` で回数を確認できる。活動が終了したと
確認できないセッションは自動で閉じないため、必要なら状態を確認して明示終了する。
同じセッションで操作が実行中なら、別の操作・終了は `session_busy` として実行前に拒否する。

クライアントが切断しても、受け付け済みの操作は Engine の操作台帳と実行タスクで追跡する。
結果が不明な操作を新しい操作 ID で再送してはならない。元の操作 ID が分かる場合に
`operations_get` で回収する。元の ID が不明なら、成功・失敗を推測して再実行しない。

Engine の終了時には保持中の環境を閉じる。Engine 再起動後にはセッションを復元せず、
古い ID は `session_not_found` になる。操作台帳には過去の開始成功が残るため、
同じ開始操作 ID の再送で得た過去の結果を、現在の生存確認の代わりにしてはならない。

終了済みの状態情報は、後処理時に最大128件へ整理する。古い情報が削除された場合も
`session_not_found` となり、新しい環境を暗黙に作り直すことはない。

## 状態と失敗

`open` は実行環境を保持している状態で、個別ツールの認証や実行成功の保証ではない。
`busy` は処理中、`closed` は明示終了、`expired` は無操作期限切れ、`unusable` は
実行環境の消失・不確定な呼び出し結果などにより継続できない状態である。

外側の操作応答の `state: completed` は、その状態照会や終了操作の処理が完了したことを示す。
`data.state` のセッション状態とは区別する。

- `catalog_stale` なら子ツールを呼ばず、セッションを維持する。定義を再検査してから判断する。
- 実行後の応答喪失や不正な結果は操作を `unknown` とし、セッションを `unusable` にして終了する。
  自動再実行・自動再起動はしない。終了は既に発生した外部への作用を取り消さない。
- 別の所有者、存在しない ID は同じ `session_not_found` とし、他接続の存在を開示しない。
- 作成時と異なる cwd は `session_workspace_mismatch` として拒否する。
- 後処理に失敗した場合は `cleanup_confirmed: false` とし、使用枠を解放しない。
  開始失敗時にも診断情報へセッション ID を残し、所有者が状態確認・終了を行えるようにする。

`cleanup_confirmed` は Anywhere Computer が所有する App Server 実行環境についての確認である。
任意の第三者プラグインが作った独立プロセスや、外部サービスの処理が全て停止したという
保証ではない。認可取消しは次のアクセスを拒否するが、アイドル環境の物理的な解放は
明示終了・期限切れ・Engine 終了まで遅れる場合がある。

## 配備と認可

追加ツールは `codex_plugin_session_open`、`codex_plugin_session_status`、
`codex_plugin_session_close` の3件。既存の2ツールには省略可能な `session_id` を追加する。

コードや配布物を更新しても、既存 grant に新しい権限を自動追加しない。
本番で利用するには新版への切り替え、ChatGPT のツール定義更新、利用する新ツールへの
明示的な認可が必要である。旧コードが稼働中なら、GitHub への push だけでは利用可能にならない。
ローカルのテスト用認可で成功したことと、本番 ChatGPT での利用確認も区別する。

## 検証

作業ツリー専用の仮想環境を使う。別の作業ツリーの Python を `PYTHONPATH` だけ変更して
使うと、環境変数を引き継がない MCP SDK の子プロセスが旧コードを読む場合がある。
インタープリターと `anywhere_computer.__file__` を確認する。

```sh
uv sync --locked --offline --python 3.12
uv run --offline python -m pytest -q
uv run --offline python scripts/verify_plugin_sessions.py --codex /absolute/path/to/codex
```

実機試験は隔離した CODEX_HOME にメモリー内カウンター MCP サーバーを登録し、
実際の Codex バイナリー、OAuth grant、HTTP アダプター、公式 MCP SDK を使う。
従来の単発実行で状態が失われること、セッション付き実行で保持されること、
HTTP 再接続・トークン更新・別 grant・同一操作 ID・古い指紋・画像中継・終了を検査する。
本番の認証情報や稼働サーバーは変更しない。

`--installed-cwd /absolute/workspace` を加えると、利用者の登録済み
`openaiDeveloperDocs.list_openai_docs` を同じセッションで2回呼ぶ追加試験を行う。
これは明示的な opt-in であり、デフォルトの隔離試験では利用者の Codex 設定を読まない。
本文は検証レポートへ保存しない。メソッド送信の記録でモデルターンがないことを検査する。

セッション維持と画像中継は Computer Use の構成要素だが、デスクトップのクリック・
キー入力・権限付与や、Codex 専用ランタイム全体との互換性を提供する機能ではない。

公式 App Server の thread / MCP 操作仕様:
https://developers.openai.com/codex/app-server/
