# Alpha9 配布・更新確認

検証対象は main の `a13d0b850191d4777718d396c35d622abc344f88`。
2026-09-13 に GitHub main とローカル HEAD の一致、作業ツリーに変更がないことを確認した。

## Windows CI と配布物

[GitHub Actions run 34735568041](https://github.com/meteosimaji/anywhere-computer/actions/runs/34735568041)
は Windows、macOS、Linux の全ジョブが成功した。

| Windows の検証 | 結果 |
| --- | --- |
| ネイティブ起動試験 | 11 passed、6.98 秒 |
| 端末所有権・連続操作の早期試験 | 3 passed、1 skipped、2.02 秒 |
| 2 worker の全件試験 | 881 passed、27 skipped、399.74 秒 |
| 日本語と空白を含むパスへの配布 ZIP 展開後の検証 | 成功 |

配布物の検証結果は `agent_started`、`files_roundtrip`、`regex_child`、
`terminal_reconnected`、`busy_stop_refused`、`agent_exited`、
`fixture_credential_removed` がすべて true、manifest_files は 4645。
Windows では runtime-only 分岐ではなく、実エージェントの起動・再接続・終了を実行した。
CI のコマンド表示には両分岐が含まれるため、判定には実際の結果 JSON を使用した。

この結果は GitHub Windows runner での検証であり、利用者の Windows ARM64 VM、
Windows GUI、HVCI、ゲームの動作を証明しない。過去の試験とは件数が異なるため、
今回の所要時間を同一条件の速度比較として扱わない。

## インストール済み Codex Plugin

インストール済み `0.1.0-alpha.9` の `.mcp.json` にある引数と作業ディレクトリで
新しい stdio MCP プロセスを起動し、initialize、tools/list、computer_status、
devices_list を実行した。ソースの Engine を直接 import する試験ではない。

- computer_status: `0.1.0a9`、ready、authenticated-loopback。
- MCP カタログ: 65 tools。devices_list、devices_tools、devices_call を含む。
- 常駐エンジンと同じ instance/runtime ID を確認した。
- devices_list は local だけを返した。Windows VM はまだ登録・接続確認していない。
- 確認時の active terminal/plugin/direct MCP/search/operation はすべて 0。

## ChatGPT の登録更新

既存 Anywhere Computer の設定画面から「更新する」を実行した。
更新後の画面で devices_list、devices_tools、devices_call の追加を確認した。
既存の「すべてのアクションを許可」は維持されている。

これは ChatGPT に登録されたツール定義の更新確認であり、新規チャットからの
alpha9 実操作や Mac と Windows の切り替え受け入れ試験の完了ではない。
これらは引き続き未検証とする。
