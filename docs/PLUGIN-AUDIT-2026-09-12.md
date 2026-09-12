# Plugin 監査指摘への対応と検証（2026-09-12）

この文書は先行監査修正時点の記録。後続の変更・GitHub保存・最新の検証範囲は
[監査チェックポイント](AUDIT-CHECKPOINT-2026-09-12.md)を参照。

ローカルの main と GitHub origin/main は、着手時点でともに
`c074946707a7e127eda69cae547386a66213e56b`。`git ls-remote origin HEAD refs/heads/main`
でリモートを照合し、着手時の作業ツリーは変更なしだった。Web の GitHub ページは
404 だったため、リモート照合の根拠には Git の応答を用いた。

## 実装結果

| 指摘 | 変更 | 検証 |
| --- | --- | --- |
| codex_apps 経由の自己呼び出し | 提供元名前空間を正規化し、一覧・正確な選択・単発実行・セッション実行で拒否 | 修正前に回帰テスト4ケースの失敗を確認。修正後は起動・子呼び出し0回。他の提供元のドット付き名は実行可能 |
| 説明文が1,000文字で切れる | 全文を保持して検索・指紋計算し、一覧の表示時だけ短縮 | 修正前の詳細取得失敗を再現。後半の語の検索、全文変更時の指紋不一致、実機の全2,778文字取得を確認 |
| Computer Use の文脈不適合 | 接続状態と機能適合を分離し、既知の直接経路を実行前拒否 | 実機カタログの接続状態と適合表示を確認。cua_repl.js は unsupported_execution_context で拒否 |
| 失敗理由が消失する | 段階・RPC数値コード・固定分類・復旧案を操作結果に保持。stderr を容量制限付きで排出・分類 | 起動・一覧・送信後のRPC失敗、大量かつ改行なしのstderr、秘密の非保存、operations_get 回収を検証 |
| カタログ障害後の状態 | 障害で応答の対応が不確かになったセッションを unusable にする | カタログ障害は終了し、指紋不一致・実行前拒否では健全なセッションを維持することを確認 |

診断は既存のローカル操作台帳に保存する。秘密情報の識別漏れを避けるため、自由文を
一部置換して保存する方式は使わず、既知の語句を固定ラベルに変換する。元の message、
data、stderr 本文は保存しない。分類できない本文は redacted となり、詳細な根本原因を
この情報だけで特定できるとは限らない。仕様は [Codex context](CODEX-CONTEXT.md) を参照。

## 実機・公式 API の確認

Codex は `/Users/megantemeteo/Desktop/ChatGPT.app/Contents/Resources/codex` の
`0.154.0-alpha.6.2`。そのバイナリから `app-server generate-ts --experimental` を実行した。
McpServerToolCallParams には threadId、server、tool、arguments、任意 JSON の _meta がある。
_meta の存在から、正規の session_id / turn_id の発行や操作の有効化は証明できない。

[公式 App Server 仕様](https://developers.openai.com/codex/app-server/)は turn/start を
Codex 生成の開始として定義している。調査した生成定義と公式仕様では、必要な
Computer Use 文脈を非推論で用意する正式な API を確認できなかった。
架空の ID の注入や turn/start の追加は行っていない。

インストール済み Plugin を専用の一時コンテキストで読み、次を確認した。

- cua_repl の runtime_status は connected。js の説明文は2,778文字、切断なし。
- codex_apps に query=anywhere_computer を指定した自己ツールの件数は0。
- cua_repl.js の呼び出しは実行前に拒否。今回の試験で GUI 操作は送信していない。
- 試験コンテキストは終了済み。

実機結果は `output/plugin-audit-live-20260912.json`、生成 API は
`output/codex-api-audit-20260912/` に保存した（どちらも Git 管理外）。

## 検証結果

- `uv run ruff check src tests scripts`: 成功。
- `uv run mypy`: 61ソースファイル、エラーなし。
- `uv run pytest -ra --junitxml=output/pytest-audit-20260912-final.xml`:
  700 passed、5 skipped。Linux/Windows 専用4件と、使い捨て GitHub runner に限定した
  常駐登録の実機試験1件を macOS ではスキップ。
- 最後の拒否エラー分類の変更後、関連9テストファイルを再実行: 123 passed。
- `uv build`: sdist と wheel の生成成功。
- `uv run python scripts/package_plugin.py`: 同梱 wheel と checksums を更新。
  同梱物と全ソースの一致テストも成功。
- `scripts/verify_codex_plugins.py`: 実際の Codex と隔離 MCP fixture の直接呼び出し成功。
  モデル開始なし、fixture 子プロセス残留なし。
- `scripts/verify_plugin_sessions.py --codex <上記バイナリ>`: 公式 MCP SDK と認証付き
  loopback HTTP で、状態1→2、再接続・認証更新後の状態保持、重複呼び出し1回、
  古い指紋・別所有者・失効認可の拒否、セッション終了を確認。
- `scripts/verify_plugin_images.py --codex <上記バイナリ>`: 合成PNGの中継、操作台帳からの
  回収、古い指紋の実行前拒否を確認。これは実画面のスクリーンショット試験ではない。
- `git diff --check`: 成功。

## 適用範囲と未検証部分

ローカルソース・テスト・文書・同梱 Plugin 配布物まで更新した。
commit、push、インストール済み Plugin の置換、常駐サーバーの停止・再起動、
接続設定・認証権限の変更は行っていない。通常の ChatGPT Chat から更新版を使った
受け入れ試験と、Windows/Linux 実機での今回の変更の検証は行っていない。

Computer Use のクリック・ブラウザー操作を利用可能にする修正ではなく、添付監査の
非対応時の方針に従い、この直接経路では対応を確認できないことを実行前に示す修正である。
対応解放には正式な非推論 API の確認と、画面読取後の実操作による検証が別途必要。
カタログ全体2 MiB・通信8 MiBの制限を超える定義は明示エラーとなり、超過した単一
カタログの分割取得は未対応。一般 Plugin 内部からの間接 Computer Use は保証しない。
