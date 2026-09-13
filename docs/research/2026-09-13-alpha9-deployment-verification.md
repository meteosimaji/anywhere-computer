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

### Windows ARM64 VM での追加確認

同日、上記配布 ZIP を Windows ARM64 VM に転送し、転送前後の SHA-256
`ed55c0fe3de88cf593a062619a763723552ae308ac7b5f7c7835e6a6eb447642`
の一致を確認した。配布 Python は x64 版であり、ARM64 ネイティブ Python の試験ではない。

Windows 側の担当が対話ログオン環境で実行した `verify_portable.py --worker` の
結果ファイルを Mac 側から SSH で独立して読み戻した。終了コードは 0、
`agent_started`、`files_roundtrip`、`regex_child`、`terminal_reconnected`、
`busy_stop_refused`、`agent_exited`、`fixture_credential_removed` はすべて true だった。
これは配布物の非 GUI エージェント試験であり、ChatGPT から Windows への通し試験や
インストール済み Windows Codex Plugin の受け入れ完了ではない。

OpenSSH ログオンでの同じ worker は Credential Manager の `CredRead` が
WinError 1312 となった。一方、対話ログオンでの worker は成功した。
`connection.exchange()` は既存エージェントへ接続する前にも
`local_credential()` を呼ぶため、対話側エージェントを起動するだけで
SSH 側の資格情報読み取り問題が解消するとは扱えない。
既存 HTTP クライアントも証明書検証付き HTTPS と OAuth が前提であり、
単なる HTTP の SSH トンネルへの置き換えは未対応。Windows の Plugin 登録と
認証を維持した複数端末経路の検証は継続中である。

同じ VM の検証用配布 Python で `-I -m anywhere_computer --help` を実行すると、
日本語 Windows の出力エンコーディングによる `UnicodeEncodeError` で終了コード 1
となった。`-I -X utf8 -m anywhere_computer --help` は終了コード 0。
この比較は SSH から同じ実行ファイルを起動して確認した。
配布生成スクリプトの Windows 用 `anywhere.cmd` と `Setup ChatGPT.cmd` に
`-X utf8` を追加した。`-I` は Python の環境変数を無視するため、環境変数でなく
起動引数で指定する。既存 ZIP の更新・Windows インストール済み Plugin の反映を
このソース修正だけで完了とは扱わない。

### Windows インストール済み Plugin の受け入れ

Windows 側が保存した `installed-plugin-exact-acceptance-result.json`
（観測時刻 `2026-09-13T04:48:21.501671+00:00`）を Mac 側から SFTP で取得した。
試験はインストール済み `.mcp.json` の実コマンドと引数を使用している。

- `anywhere-computer@personal` / `0.1.0-alpha.9`: installed、enabled。
- initialize、tools/list（65 tools）、computer_status が成功。
- server version `0.1.0a9`、platform `Windows`。
- 専用作業領域でファイル新規作成・読戻しが成功し、MCP プロセス終了コードは 0。
- 同じ Python の `--help` 比較も、`-X utf8` ありは成功、なしは失敗。

この記録のホスト名は Mac の SSH 接続先と一致した。ただし SSH 側では
報告されたユーザー領域の `Programs` ディレクトリを取得できない一方、
プロセス一覧にはその配下の Python 実行ファイルが表示された。
追加調査で、Windows Codex の MSIX による AppData 転送を確認した。
SSH からパッケージの `LocalCache/Local/Programs/AnywhereComputer` に
本体が存在し、その実体の Python を起動して `0.1.0a9` / Python `3.12.13`
を取得できた。Codex 内の論理パスと、パッケージ外の実体パスが異なっていた。
この配置は Codex の私有データに依存するため、独立した永続導入としては
AppData 外のユーザー専用配置先へ調整が必要。既存状態の移行は未実施。
Microsoft の [MSIX デスクトップアプリ実行仕様](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes)
に AppData の転送が説明されている。
対話ログオンでの Plugin 受け入れと、SSH／ChatGPT から同じエージェントへの
接続成功は別の判定で、後者はまだ未達。

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

## 更新後の新規 ChatGPT 試験

更新後に新規チャットを作成し、モデル選択画面で GPT-5.6 Sol、推論の深さは
中程度を確認して試験した。computer_status は `0.1.0a9` を返し、
インストール済み Codex Plugin と同じ instance/runtime ID だった。

- devices_list、devices_tools、devices_call による local の状態取得が成功。
- 日本語・絵文字を含むファイルの作成、読取り、取得したハッシュを条件にした
  追記、再読取り、検索が成功。
- 同じ端末セッションへの連続入力、状態・出力の回収、停止が成功。
- Skill の一覧と本文、Plugin のカタログとスキーマ取得が成功。
- node_repl で変数を 40 に初期化する呼出しが成功し、その結果を
  operations_get で回収した。最後に Plugin セッションを閉じた。

続く同一 Plugin セッションでの `40 → 42` の更新は、ChatGPT 側に
OpenAI の安全性チェックによるブロックが表示されたため未達。
別経路による回避や再送は行っていない。

2026-09-13 の常駐エンジンの操作台帳を読み取り専用で照合した。
HTTP の grant ごとの名前空間を実装どおり SHA-256 で解決すると、
外部操作 ID の末尾 `0016`（初期化）、`0018`（結果回収）、`0019`
（セッション終了）は completed、`0017`（更新）は記録なしだった。
この区間の記録は ChatGPT の報告と一致し、更新がサーバーで実行された証拠はない。
認証情報、grant ID、私用ファイルの内容は公開記録に含めない。

Computer Use と Windows VM の切り替えは、この新規チャットでは未検証。
この結果を全機能の受け入れ合格とは扱わない。
