# ChatGPT監査パッチのローカル統合

対象HEAD: 628e42ecac6c181af2c072edffe966cec32d46fa。
提供元: output/chatgpt-audit-20260913-UT7P7E/anywhere-alpha7-audit-fixes.patch。
既存の未コミット資料と連続作業試験は維持した。

3件のソース修正と11件の回帰テストをレビュー・取り込み。
MCPのOS起動拒否では枠を解放し、予期しない例外ではcleanup未確認を維持する。
端末は起動の検査から登録までをロックし、同時要求による32枠超過を防ぐ。
端末入力開始後の送信・観測例外はunknownとして台帳に残し、再送しない。
例外本文を保存せず固定分類と試行byte数を返す。送信前拒否はfailedのまま。

検証:

- 修正前に提供された11件を実行: 8 failed / 3 passed。不具合を独立再現。
- 適用後の監査・端末・直接MCP関連: 30 passed。
- 通常の連続作業とHTTP能力試験: 2 passed。
- Ruff: src/tests/scripts成功。mypy: 77 source files成功。
- 全件: 900 passed / 5 skipped / 1 failed、88.79秒。失敗は同梱wheelと修正ソースの不一致。
- scripts/package_plugin.pyでwheel・Plugin ZIPを再生成した後、配布物照合と監査試験:
  12 passed。再生成後の全件試験は反復していない。git diff --check成功。

4件目の子プロセス問題は未修正。提供された実プロセス再現を実行し、親終了後の子が
terminal_stop後にも生存、update_blocked=false、stop state=exitedを確認。
再現コードのfinallyで専用の子を終了し、fixture_child_cleanup_confirmed=trueを確認した。
単なるkillpgの条件削除ではPID再利用・所有関係の問題を解決できない。
残る作業は、親より長寿命の子の所有関係・停止結果・容量・更新ブロッカーを一貫して管理し、
実プロセスとPID再利用を含む試験で検証すること。GUI機能追加より優先する。

この統合は監査全体の完了ではない。commit/push・稼働版への適用・権限変更は実施していない。
生成物のvalidationはunverified、source_dirty=trueであり、検証済みstableリリースではない。

## alpha8での後続修正

上記は3件を取り込んだ時点の記録。続くalpha8では専用の所有プロセスを導入し、
親シェルが終了しても同じPOSIXプロセスグループ/Windows Jobの子が残る間は所有元を保持する。
親終了後の子の停止・更新ブロッカー維持、SIGTERMを無視する子の終了、終了後に古いPGIDへ
送信しないことをtests/test_terminal_children.pyに追加した。従来の未修正記述を更新した。
正式な最終結果と配布・接続版の照合はalpha8の更新検証記録に記載する。
