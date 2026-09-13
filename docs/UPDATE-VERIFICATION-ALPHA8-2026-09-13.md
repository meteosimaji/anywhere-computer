# alpha 8 修正・更新検証記録

確認日: 2026-09-13。実装コミットは8039c2c、配布物のクリーンなソース由来情報を
記録したコミットはed9164f。以下は開発版alpha 8の検証であり、stable認定ではない。

## 監査で再現した4件

| 不具合 | 修正と検証 |
| --- | --- |
| MCPのOS起動失敗で4枠が残る | 起動失敗時の枠回収と回帰試験 |
| 同時端末起動で32枠を超える | admissionから起動・登録までのロックと競合試験 |
| 入力開始後の失敗が再送可能なfailedになる | unknownとして台帳へ記録し、同じ操作IDでは再送しない試験 |
| 親終了後の子が停止・容量・更新判定から消える | POSIXグループ/Windows Jobの所有プロセスを保持。実プロセスで子の生存・停止・更新ブロッカーを検証 |

提供された11件の試験は修正前8 failed / 3 passedで独立再現し、修正後に通過した。
子プロセス試験ではSIGTERMを無視する子の終了と、完了後の古いPGIDに信号を送らないことも確認した。
POSIXで意図的に別セッションへ離脱したデーモンや、エージェント自体の異常終了後の復元までは保証しない。

## 最終ソースと配布物

- macOS全件: 907 passed / 5 skipped、93.52秒。
- Ruff: src / tests / scripts成功。mypy: 79ソース成功。Windows対象のworker型検査も成功。
- 同梱wheelのソース・チェックサム照合成功。配布物のsource_dirty=false。
- macOS portableの別ディレクトリへの展開、実起動、ファイル往復、正規表現子プロセス、
  端末再接続、作業中停止拒否、正常終了、試験用資格情報の後片付けを検証。
- 最終配布ディレクトリでも3113ファイルの整合性とruntime試験を再確認した。

HTTP経由の機械GUI試験は、インストール済みPeekabooへの直接MCP接続で実行した。
新しいgui_observe / gui_key / gui_typeを使い、Calculatorの42→50→51→52→53→54を
画面再観測で確認、6回の結果再取得とセッション終了まで成功した。
47 API呼出しはすべてcompleted。観測には表示の反映待ちがあり、各段階2〜5回の観測を使った。
これはモデル判断を含まない機械試験であり、ChatGPT側の判定や所要時間とは別である。

gui_clickはHTTP統合試験で検証しているが、このCalculatorの機械実機試験では使用していない。
全ツール契約のHTTP試験と、外部サービスすべての実機試験は同義ではない。

## 稼働版への反映

共有エンジンとChatGPT用常駐HTTP起動元をalpha 8 portableへ変更した。
既存の接続URL、OAuth資格情報、操作台帳を維持し、旧alpha 7配布ディレクトリも保持した。
既存フルアクセス設定に新規GUIツールを同期した。独自の承認画面は追加していない。

Codex公式CLIでanywhere-computer@personalを再インストールし、
0.1.0-alpha.8の実際の.mcp.jsonから新規stdio接続を開始した。
65件のconnectorツールと4件のGUI定義を取得し、computer_statusが以下を返した。

- version: 0.1.0a8
- instance_id: 67c3dd2de9e64b2785ff40667ffc4a90
- runtime_id: 87f2aa0555b66b8770651bc81fb91ac7deae7fecb2a4ec97d6f3df99c0487242
- engine API: 1、engine tools: 59

ChatGPT設定の既存Anywhere Computerで「更新する」を実行し、gui_observe、gui_click、
gui_type、gui_keyが表示された。既存の「すべてのアクションを許可」設定も確認した。
新規チャットではGPT-5.6 Solを選択し、送信前に「5.6 中程度」を確認した。
最初の実computer_statusは上記と同じ版・instance・runtimeを返した。

新規Solチャットでは日本語・絵文字の作成、読戻し、SHA-256付き追記、Skill一覧、
mcp_toolsのsummary/query/nameの実使用が成功した。直接MCPセッション
4a2bc5487e1f462f9b490c8cae6800f0で計算機へ12+30を入力し、再観測で42を確認した。
サーバー台帳の観測操作3458d39f9dea97e09ab95d7dd1e258cdでも12 + 30と42を照合した。
終了操作b5b0b2b041f7f905303cc23f87444d36はclosed / cleanup_confirmed=true。
この試験ではChatGPT側の安全性拒否はなかった。

ただし初回はキー名ESCAPE/ESC/escが入力仕様で拒否され、gui_keyには手戻りがあった。
後続修正で大文字・小文字を正規化し、esc/enterの別名を追加した。3ケースを修正前に
失敗再現してから修正。許可キーをschemaに列挙した。アプリ名のローカライズについては
MCPの起動結果の名前を使うことをschemaへ追加し、自動で別アプリを選ぶ実装は追加しない。
状態にはgui_mcp_adapterの登録・前提条件・runtime未検証を追加し、内蔵gui=falseと区別した。
入力値が不正な場合は観測を消費しないため、キー名修正だけなら再観測は必須ではない。

後続修正のローカル全件は910 passed / 5 skipped、92.90秒。Ruff/mypyも成功。
修正版の配布元はc2259d8（ソース1e489f1）。Codexインストール先から新規stdio接続で
更新後のキーschemaと65件のカタログを照合した。ChatGPT設定でも更新し、同じSol中程度
チャットで短い再試験を行った。実測値は以下。

- version: 0.1.0a8
- instance_id: 86a297dc36234360838039ec0818d957
- runtime_id: 1e83184837cc234619cd6a2e0d6edb29671acd6b41a8a0e606a6c35ff7b6140d
- gui_key操作6c1e6c37726b04f77ed03954e3d5fbb8: Pressed escape、is_error=false。
- 続く観測9d136a8f4efa3c1bb8dd118fca13e550: 表示0。
- 入力後の観測875d492fcfac7a7f94d385d9d2d35a87: 12 + 30と42。
- 終了e155413f74e545c4d67d124f81c74898: cleanup_confirmed=true。

上記はChatGPTの回答だけでなく、稼働エンジンの操作台帳からも照合した。
最終statusでは全種類のactive resourcesが0、update_blocked=false。
GUIアダプターのruntime_verified=falseはstatusが自動の外部MCPプローブを行わないことを示す。
個別GUI試験の成功を否定するフィールドではない。観測を各操作の間に挟む方式は継続している。

Windows CIの初回は新しい回帰試験内で停止したため、旧実行を中止してログを回収した。
cmd.exeへ渡す試験用Pythonの複数行を1物理行のexec表現へ変え、EOF後にもdeadlineへ
制御が戻るよう試験ループを修正した。製品workerは変更していない。
192625aでは重点試験を全件より先に実行し、macOS/Linux/Windowsすべてで成功した。
続くファイル編集・対話端末の試験では、Windowsのcmd.exeがPythonコード中の引用符と
READY>を解釈して構文を破壊する別のfixture問題が残った。失敗時のstdoutを記録し、
PythonのSyntaxErrorと欠落した文字列を確認した。4984567ではWindows用python_commandを
UTF-8のhexから復元する一行に変更し、複数行・日本語・引用符・シェル特殊文字の実行試験を追加した。
重点試験の先行実行により、この失敗は全件試験を待たず数秒で検出できた。

Windowsの全件試験は2 worker / loadfileへ変更した。直列192625aでは714.38秒、
並列e0af498では350.99秒だった。両者とも同じfixtureの1 failed / 876 passed / 27 skippedであり、
この比較自体は最終合格の記録ではない。OSネイティブ常駐登録は別途直列実行し、
認証KDFや30秒のHTTP待機試験を短縮・省略する変更はしていない。
4984567の[CI](https://github.com/meteosimaji/anywhere-computer/actions/runs/34733975347)は
macOS/Linux/Windowsすべて成功。Windowsは全件878 passed / 27 skipped、342.94秒、
別工程のネイティブ常駐登録は11 passed。スキップにはPOSIX専用fixtureを含み、全OS・全外部Pluginの
実機成功を意味しない。Windows x64 runnerでの結果であり、Windows ARM64 VMは別途検証する。

このCIの移設portable試験はfiles_roundtrip=true、regex_child=true、native_agent_tested=false。
Windowsで追加するつもりだった完全検証は、GitHub式の空文字が偽扱いされてruntime-onlyになっていた。
後続ではシェルの明示的なifへ修正する。この実行をportableのネイティブ接続・再接続成功とは扱わない。

## 比較対象と能力の境界

[DesktopCommanderMCPの実コード確認](REMOTE-DESKTOP-COMMANDER-CODE-REVIEW.md)では
0.2.50 / a781f5a4を固定して確認した。プロセスの出力からの終了推定を採用せず、
Anywhereでは実際の所有プロセス群を管理する。直接MCPの要約・検索・個別schema取得も追加した。
検索フィルターは現在の1ページにだけ適用するため、nextCursorがあれば継続が必要。

GUIは明示的に選択したPeekaboo MCP用アダプターであり、Codex専用Computer Useの外部実行文脈を
有効化したわけではない。computer_status.capabilities.gui=falseは内蔵GUI能力がないことを表す。
外部MCPアダプターの登録・起動・実操作成功はそれぞれ分けて確認する。
