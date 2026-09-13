# ファイル以外の受け入れ確認

追記: 後続のChatGPT監査で、正常系試験では検出できなかったMCP起動失敗時の枠残留、
端末同時起動の上限競合、入力送信後の不明結果の誤分類、親終了後の子プロセス残留を再現した。
本報告の成功は以下の試験条件に限る。特に子プロセス群の停止と更新判定が一般に正しいことを
証明しない。子プロセス残留は後続のalpha8で修正し、[端末の既知事項](../TERMINAL.md)に記録している。

2026-09-13、macOS。常駐版の直接呼出しと、ソース版の隔離試験を区別する。
本番設定・権限・配布版は変更していない。GUI試験では電卓を前面化し表示を54にした。

## 常駐Pluginの実動作

computer_status: 0.1.0a7、55 tools、authenticated-loopback、ready。
runtime_id: da8434b156b5e6c3b8af8260bcea11b3da750042a6ff21a3301d93568a41a13a。
公開HTTPS経由のChatGPT試験ではない。

- codex_plugin_session_open → node_repl.jsのschema取得 → 変数40を定義 →
  同じ変数に2を加算し42 → operations_getから42を再回収 → status=open → close。
  cleanup_confirmed=true。モデル生成要求はしていない。
- 42を返した操作ID: 147b8751e0d4466e9c7c2bd68978eaea。
- terminal_startで専用/bin/catを起動し、日本語・絵文字を含む2回の入力を同じセッションで
  回収。cursorは0→25→42 bytes、dropped_bytes=0、両方prompt_matched=true。
  各入力応答のelapsed_msは21。これは入力後の待機時間で、通信全体の時間ではない。
- terminal_stopで専用プロセスを終了。最終statusのterminal/plugin/direct MCP/operationは
  すべてactive=0。停止のexit_code=-15は要求したSIGTERMによるもの。

## ソース版の隔離試験

次の関連試験は41 passed in 2.04s。

```sh
.venv/bin/pytest -q tests/test_direct_mcp_sessions.py tests/test_plugin_sessions.py \
  tests/test_processes.py tests/test_skills_context.py tests/test_http_supervisor.py
```

直接MCPの実stdioセッション・画像回収、Pluginセッション契約、PIDと起動時刻を照合する
プロセス終了、Skill解決、常駐監督の再起動制御を対象とする。fixtureや故障注入を含み、
すべての外部Pluginを実行したという意味ではない。

scripts/verify_plugin_sessions.pyも実際のインストール済みCodexバイナリと隔離counter MCPで成功。
HTTP再接続・token refresh後の状態保持、操作ID再送のdispatch一回、古いcatalogの送信前拒否、
閉鎖セッションの非再作成、画像中継、後片付けを確認。記録されたRPCにturn/startはない。
この試験は本番認証や設定を変更しない。
ローカル証跡: /tmp/anywhere-nonfile-plugin-verification-20260913.json。

## 独立MCPによるGUI実操作

scripts/verify_gui_http.pyを既存Peekabooと既存OS権限で実行。
経路は隔離authenticated HTTP → direct MCP → Peekabooで、Codex専用Computer Useではない。
電卓の観測→入力→再観測を継続し、42→50→51→52→53→54を確認。
各入力は一回のみ。UI反映待ちは観測だけを繰り返す。観測回数は4/5/5/5/5/5。
6回の観測結果をoperations_getで再回収、終了時cleanup_confirmed=true。

HTTP呼出し50回、うちmcp_call 38回。mcp_callの中央値168.835ms、最大520.5ms。
ChatGPTのモデル判断時間・公開ネットワーク時間を含まない。追加のモデルは呼び出さない。
ローカル証跡: /tmp/anywhere-nonfile-gui-verification-20260913.json。

## 残る確認範囲

Codex専用Computer Useの外部認証制約とChatGPT側の安全性判定は今回再試行していない。
ブラウザーDOM・既存ログイン利用、実ネットワーク断・sleep復帰、他OS実機、全外部Pluginは未検証。
常駐statusのgui=falseは引き続き返る。外部MCPのGUI実行成功と、組み込みGUI能力の表示は別。
Skillsは今回fixture試験であり、実インストール済みSkill本文の再取得は行っていない。
