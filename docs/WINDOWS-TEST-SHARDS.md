# Windows Quality の分割と計測

Issue [#219](https://github.com/meteosimaji/anywhere-computer/issues/219) の
残存対応。production の scrypt、実 KDF テスト、Windows ACL、既存の assertion は
変更しない。Windows の残りの pytest を別々の VM に分け、各 VM の worker 数は
従来の 2 のままとする。

## 配分の根拠

[PR #230 の Quality run](https://github.com/meteosimaji/anywhere-computer/actions/runs/36583627634)
では remaining-suite が 1,092 秒だった。JUnit は 2,593 件、合計 2,166.536
テスト秒で、authorization 227.868 秒、Subchat HTTP image 218.359 秒など、
複数のファイルが負荷を占めた。これらは setup/call/teardown の合計であり、
production KDF や ACL 処理だけの時間ではない。

`scripts/ci/windows_test_costs.json` はその公開 JUnit のファイル別集計である。
現在収集した各ファイルの件数に比例させた時間を用い、重いファイルから
4 つの shard の推定合計が近づくよう割り当てる。履歴のないファイルには
既存の正の 1 件当たり時間の中央値を使用する。

履歴は配分の参考にしか使わない。実行対象は現在の pytest collection から取得するため、
新しいファイルやパラメーターを追加しても履歴 JSON への追記は不要である。
履歴上の合計が均等でも実際の終了時刻は保証されない。各 runner の速度、file 単位の
割当、2 worker 間の分配、fixture、queue、環境準備によって所要時間が変わる。

## Quality の依存関係

```text
windows-preflight ──→ windows-test-shards (1, 2, 3, 4; 各 2 workers)
         └────────────────────────┬─────────────────────┘
                                  ↓
                 shared-runtime (windows-latest)
                 実行記録検査 → build → portable 検証
                                  ↓
                         attestation / publish
```

- `windows-preflight` は workspace JS、native、owner pipe、terminal、shared-agent
  recovery を、以前と同じ独立した pytest process で直列実行する。
- 全 collection に対して、直列グループと 4 shard の和集合・重複なしを検査する。
  shard 間では同一ファイルを分割しない。workspace の 1 件だけは元の直列実行を
  維持し、同ファイルの他のテストは所属 shard で実行する。
- 各 shard は配布された plan と自分の実 collection を再照合してから、
  `-n 2 --dist loadfile --max-worker-restart=0` で実行する。
- pytest の `pytest_runtest_logfinish` から実際に終了した node ID を記録する。
  skip も数えるが、未実行・重複実行・収集のずれ・失敗した process は合格にしない。
- 最後の job は `shared-runtime (windows-latest)` という既存の必須 check 名を維持する。
  `always()` で依存 job の失敗・skip も検査し、全 shard と直列テストが成功していなければ
  自身を失敗させる。成功時にも全終了記録の和集合を検査してから build を始める。
- Linux/macOS の `shared-runtime` は Windows job を待たない。既存の他の必須 check 名、
  PR の version guard、main の既存 publish guard を維持する。
- `attest-portable` と `publish-release` は Windows の合成 job の成功も必要とする。
- 既存の `windows-feature-smoke` は早期診断として同じ一部のテストを別途実行する。
  「各 1 回」の証明対象は full-suite の直列グループと 4 shard であり、
  この意図的な smoke 再実行は含まない。

pytest collection/log hooks は [pytest の公開 API](https://docs.pytest.org/en/stable/reference/reference.html)、
file 単位の worker 分配は [pytest-xdist の loadfile](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)、
依存 job の成功・失敗処理は [GitHub Actions の needs](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idneeds)
に基づく。現在の lock は pytest 9.1.1、pytest-xdist 3.8.0 である。

## 証拠と終了条件

`pytest-results-windows-latest` artifact に、plan、グループ別の JUnit と終了記録、
KDF/ACL の独立計測、`windows-test-summary.json` をまとめる。失敗した各 job の
個別 artifact も保持する。

summary は次を分けて示す。

- `serial_process_seconds`: 直列 pytest process の合計。
- `slowest_shard_process_seconds`: 4 shard のうち最長の pytest process 時間。
- `all_shard_process_seconds`: 4 VM の pytest process 時間の合計。
- `test_process_critical_path_seconds`: 直列合計と最長 shard を加えた比較用の値。
- `test_process_total_seconds`: 直列と全 shard の process 時間の合計。

これらには queue、VM 起動、checkout、uv sync、collection 照合、build、attestation
を含めない。GitHub の job 開始・終了時刻から、Windows の全体の待ち時間と
Windows job の合計実行時間も記録する。4 台で並列化すると待ち時間を短くできる
可能性がある一方、環境準備の重複や同時 VM 数によって合計使用時間が増える場合がある。
runner の請求・無料枠はこれらの pytest 時間から推定しない。

最初の PR と main の 2 回で、full-suite の欠落なし、元の security assertions、
全必須 check の成功を確認する。最長 shard の時間を元の 998 秒、および KDF 修正後の
1,092 秒と比較し、追加されたテスト件数の違いも併記する。全体待ち時間と VM 合計時間も
両方報告する。安定性と実際の短縮が確認されるまでは #219 の完了や短縮率を断定しない。
