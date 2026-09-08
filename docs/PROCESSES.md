# プロセスと利用統計

`processes_list` は PID/name/created/status、RSS byte、累積 CPU user/system 秒を返す。
コマンドライン引数や環境変数は一覧に含めない。瞬間 CPU 使用率ではない。
`after_pid` と `limit`（既定200、最大2000）で PID 順に走査する。
参照不能/終了済みの項目は unavailable に数え、next_pid は走査済み位置を示す。
プロセス一覧は変化するため、ページ間で固定スナップショットを保証しない。

`processes_stop` は pid と、一覧で取得した created を必須とする。
起動時刻が一致しなければ終了要求を送らない。既定は terminate、force=true は kill。
3秒以内に終了を確認できなければ termination_requested と返し、成功した終了と区別する。
自身と祖先プロセスはこのツールでは終了できない。OS権限を越える操作は行わない。
試験はテスト専用の子プロセスだけを対象とする。

`usage_stats` はローカル operation 台帳を tool/state ごとに集計する。
再配送された同一 operation ID は重複計上しない。課金回数、全クライアントの API 呼出し回数、
実行時間、CPU利用率の統計ではない。照会自身も operation として記録される。
