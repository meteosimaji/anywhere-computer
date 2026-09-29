# 残存バックログと競合比較の受け入れ条件

2026-09-29 UTC に b31 のコード・稼働エンジン・GitHub issue を照合した。
この文書は現在の受け入れ条件を整理する。`OPEN-ISSUE-PLAN.md` の過去の記録は
当時の版と環境の証拠として保持し、新しい配布版の合格とは読み替えない。

## 競合から採り入れる機能

| 比較先・一次資料 | 比較した機能 | Anywhere の実装・改善 | 残る差分 |
| --- | --- | --- | --- |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp)、[Frames](https://playwright.dev/python/docs/frames)、[Locators](https://playwright.dev/python/docs/locators) | 構造化 snapshot、iframe、open shadow DOM、タブ、ダイアログ。CLI と短い用途別操作による応答量の削減も説明する | 構造と任意画像、意味的な対象、入力前の一意性検査。今回は frame ID を指定した観測・操作と open shadow labels、同じ URL の再読み込みの検出を追加 | 既存ユーザータブへの明示的結合、popup/tab 管理、ダイアログ、応答量の実測 |
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/tool-reference.md) | ページ・入力・通信・console・performance trace | 現在 DOM、通信の状態と失敗、console、出典を短い観測として取得 | レスポンス本文の限定取得、performance trace、記録の保存とモデル入力の分離 |
| [Stagehand caching](https://www.browserbase.com/blog/stagehand-caching) | ページの一致を確認した selector 再利用による推論省略 | Anywhere は取得済みの正確な role/label を再解決して入力する。操作の再送は operation ID で抑制 | 推論結果のキャッシュは未実装。サイト状態・アカウント・入力条件を含む検証と実測を先に行う。競合の短縮率を転用しない |
| [Computer Use](https://learn.chatgpt.com/docs/computer-use)、[Peekaboo](https://github.com/steipete/Peekaboo) | アプリ構造と画像、ウィンドウ、キー・スクロール・ドラッグ | Mac の AX semantic target、許可・所有者・観測 ID。今回の追加ウィンドウ画像と AX action の結果を配布時に照合する | native key/drag の正確な配送、Windows UIA、Linux AT-SPI、画面動画と連続音声のクライアント受信 |
| [ChatGPT Library](https://help.openai.com/en/articles/20001052-file-storage-and-library-in-chatgpt) | ファイルの保持と再利用 | 最大10ファイル・40 MiBを1回の準備セッションでアップロード。ファイル別 receipt と同じ ID で回復し、全件確認後にだけ添付情報を返す | 実際の複数ファイル読取、別端末への保存、アカウント切替の実機受け入れ |

画像はブラウザが描画した画面であり、DOM/AX は操作の意味を伝える構造情報である。
両方を取得しても取得時刻は完全には一致しない。frame の要素枠とタブ全体画像の
座標系も異なる。意味的な操作を優先し、観測の更新と事後の効果確認を残す。
競合より優れているという包括的な結論や、利用量の削減率はまだ主張しない。

## Issue 別の終了条件

| Issue | 現在の変更・証拠 | 終了までに必要なこと |
| --- | --- | --- |
| [#180](https://github.com/meteosimaji/anywhere-computer/issues/180) 子への制限付き権限 | 子/owner/device/tool/root/期限の検査、HTTP bearer と SSH の契約・回帰あり | 普通の Chat の子 identity/handoff と、別の実端末で許可/拒否/期限/取消/再接続を確認。登録済み2端末は今回の fresh probe でも unreachable |
| [#181](https://github.com/meteosimaji/anywhere-computer/issues/181) peer mailbox | durable delivery/ack、identity lease、重複・再起動・owner分離と過去の Codex/Claude nonce 実証あり | 親と Subchat の実モデル到達、session/account の変更、実際に使ったモデル量を記録。mailbox への保存はモデルへの割込ではない |
| [#185](https://github.com/meteosimaji/anywhere-computer/issues/185) Subchat 使い勝手 | [全項目監査](SUBCHAT-BACKLOG-AUDIT.md)。複数添付・期限・全件 preflight・不明時停止を今回追加 | 新配布版で複数添付、HTTPS grants、別端末保存、Windows/Linux background/focusを検証。provider stop/steer とレビュー済み interrupted continuation の契約は別途実装が必要 |
| [#198](https://github.com/meteosimaji/anywhere-computer/issues/198) passkey | 実 Chromium 仮想認証器・UV・CSRF・取消競合等のテストあり | 公開 HTTPS と物理/プラットフォーム認証器で owner 本人が登録・承認。パスワードや登録 ticket を Chat/公開ログに保存しない |
| [#219](https://github.com/meteosimaji/anywhere-computer/issues/219) Windows CI | [PR #230](https://github.com/meteosimaji/anywhere-computer/pull/230)。production KDF を保持し状態遷移テストだけ軽量化。ローカル17.18秒→1.87秒。Windows のKDFとACL処理を分離計測 | Windows full-suite をPRとmainで繰り返し、直近 b31 の1,075秒と比較。ローカル短縮率をWindows全体の削減率にしない |
| [#220](https://github.com/meteosimaji/anywhere-computer/issues/220) main 保護 | 必須6 checksをGitHub Actions appに固定。strict、管理者にも適用、PR必須、force/delete禁止。#230はauto-merge指定後もCI中はOPEN/BLOCKEDで未merge | 同一PRが全6件成功後にだけmergeし、mainのattestation/publication gateが成功することを確認 |
| [#221](https://github.com/meteosimaji/anywhere-computer/issues/221) 公式 CUA 直接経路 | 実カタログは connected/3 tools でも unsupported_execution_context。公式資料と[直接経路の境界](CODEX-CONTEXT.md)を更新 | 普通の Chat から正式なCodexターンを起動せず、認可済みAnywhereのGUI/browserを使う代替を実証。公式APIが確認できるまで unsupported を保持するというissueの代替受け入れ条件を適用 |
| [#222](https://github.com/meteosimaji/anywhere-computer/issues/222) 検索と効率 | native検索→正確なページ→必要時GUIの方針。出典/構造/通信の観測とframe制約あり | 同一課題で1 Chat対3 Subchatの時間・重複・品質・実 usage を計測。不明な usage は unavailable。子の数をプロンプト文字列から制限しない |
| [#224](https://github.com/meteosimaji/anywhere-computer/issues/224) 一時タブ終了 | [調査と回帰](CHAT-TAB-CLEANUP.md)。Playwrightのclosing状態では再closeが命令を再送しない。所有targetだけをbounded CDP retryし、close eventを確認 | full Qualityと繰返しmacOS CI。元CIの自然発生原因は未確定。元タブを保持し、cleanupのエラーで読取結果を上書きしない |

## 配布の終了条件

- PR Quality 全件成功、main の build/attestation/publication 成功を別に確認する。
- release ZIP の版・checksum・内容と、導入済み Plugin の版・wheel hashを照合する。
- 所有者の活動中セッションがないことを確認して、常駐エンジンを更新する。
- local と認証済み通常 Chat が同じ新 instance/runtime を報告することを確認する。
- 新しい tool の scope を含む通常 Chat から、実入力と画像/音声の受信を検証する。
  ソースのテスト成功を、権限・配布・モデル受信の成功に置き換えない。

現在の b31 配布物は audio helper を含まず、実エンジンも unavailable と報告した。
既存の macOS 15+ system playback helper を配布工程へ組み込む修正を進める。
permission check が成功しても、音を録音した・モデルが聞いたという証拠ではない。
