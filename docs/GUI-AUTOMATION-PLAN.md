# GUI 自動操作の到達点と実装計画

2026-09-28 時点。実装・導入済みエンジン・実機受け入れを区別する。

## 比較に使った実装と資料

| 系統 | 確認した機能・課題 | Anywhere Computer への反映 |
| --- | --- | --- |
| OpenAI Computer Use | この Mac のプラグインは proprietary なマニフェスト、署名済み `SkyComputerUseService` と実行時 API を提供する。ソースコードは同梱されない。実行時 API では AX 状態・差分、画面画像、要素番号でのクリックと値設定、二次 AX action、スクロール、ドラッグ、キー入力、既存ブラウザタブへの結合を確認した。 | 実コードを閲覧したという主張はしない。操作契約を比較対象にし、Anywhere 独自の許可と操作台帳を保つ。 |
| Peekaboo | 公開コード・文書には要素 ID／文字クエリ／座標、AX action 優先、画面画像、スクロール、メニュー、ドラッグ、ウィンドウ固定、移動したウィンドウの再検証がある。 | 既存の任意 MCP アダプターは残し、内蔵 macOS 経路ではまず正確な AX 役割・ラベル・識別子を使う。 |
| Playwright MCP / Playwright | アクセシビリティ snapshot を基本の操作情報に使い、画像は視覚確認のため別に取得する。snapshot に表示領域基準の枠を含められ、Locator は DOM の再描画後に対象を解決し直す。大きな snapshot の応答量は公開 issue にもある。 | 隔離ブラウザで要素 handle 固定をやめ、意味的対象の再解決を使用する。短い要素一覧を標準とし、画像は要求された時に返す。 |
| browser-use | 公開コードは DOM・AX・レイアウト snapshot と画面画像を組み合わせる。DOM の直列化と画像のモデル入力は別処理。公開 issue には古い要素番号や座標クリック時の診断欠落がある。 | 要素番号・座標だけに依存せず、曖昧さを入力前に拒否し、失敗理由と観測更新を返す。 |
| Stagehand | `observe()` は説明、推奨操作、XPath を返し、`act()` に渡せる。iframe と shadow DOM に対応する。 | 一覧と実行を分ける契約は参考にするが、モデルの提案を無確認で実行せず、所有者と観測 ID を固定する。 |
| Skyvern | 公開資料では Playwright と AI 操作を併用し、AI 操作時に画面画像を使い、実行記録には画像と DOM tree を残す。 | 事後の画像・構造記録は有用。毎回の画像取得はコストが増えるため、既定では短い構造情報にする。 |
| Anthropic computer toolset | 画面取得・マウス・キーボードをクライアント実行環境で組み合わせる契約。 | モデル側の推論ループを Anywhere エンジン内に複製しない。Chat モデルが、認可されたツールを直接選ぶ。 |

参照: [OpenAI Computer Use](https://learn.chatgpt.com/docs/computer-use),
[Peekaboo click](https://github.com/openclaw/Peekaboo/blob/main/docs/commands/click.md),
[Peekaboo CLI](https://github.com/steipete/Peekaboo/blob/main/docs/cli-command-reference.md),
[Playwright MCP](https://github.com/microsoft/playwright-mcp),
[Playwright locators](https://playwright.dev/docs/locators),
[Playwright actionability](https://playwright.dev/docs/actionability),
[Playwright MCP の snapshot と screenshot](https://github.com/microsoft/playwright-mcp#tools),
[Playwright MCP の snapshot 量の issue](https://github.com/microsoft/playwright-mcp/issues/1233),
[browser-use の DOM 実装](https://github.com/browser-use/browser-use/blob/main/browser_use/dom/service.py),
[browser-use の画像入力](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/prompts.py),
[browser-use の要素番号 issue](https://github.com/browser-use/browser-use/issues/286),
[browser-use の座標診断 issue](https://github.com/browser-use/browser-use/issues/5365),
[Stagehand observe](https://github.com/browserbase/stagehand/blob/main/packages/docs/v3/references/observe.mdx),
[Skyvern のブラウザ操作](https://github.com/Skyvern-AI/skyvern/blob/main/docs/developers/browser-automations/overview.mdx),
[Skyvern の実行 artifact](https://github.com/Skyvern-AI/skyvern/blob/main/docs/developers/debugging/using-artifacts.mdx),
[Anthropic computer use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool).

## 現在のコード

- macOS 内蔵 GUI は `native/macos/AXHelper.swift`。正確な bundle ID、プロセス起動時刻、ウィンドウ handle、60 秒の観測 ID を結び付ける。AXPress と AXValue 置換を持ち、操作前にプロセス・ウィンドウ・値を再確認する。今回、AX 役割と観測したラベル／識別子で一意の要素を指定する経路、幅優先の短い対象一覧を追加した。`AXWindows` が空のアプリは、同じプロセスの focused/main window を補助的に調べる。
- 隔離ブラウザは `src/anywhere_computer/browser_control.py`。認可された所有者の一時タブで URL・アクセシビリティ snapshot を取得し、役割／名前／ラベルを使う。今回、ページ再描画後の locator 再解決、短い表示待機、改行を整えた HTML／ARIA ラベル一覧、CSS pixel の要素枠、要求時の実際の Chromium 表示領域画像を追加した。画像は MCP image item に投影し、テキストへ base64 を複製しない。
- 任意の Peekaboo／Cua MCP アダプターは `src/anywhere_computer/gui_mcp.py`。サーバーごとの schema と実行時利用可否を別に確認する。Codex 専用 `cua_repl` のターン認証は継承しない。

## 次の実装順

ブラウザでは画像と「レンダリング」を二者択一にしない。スクリーンショットが
Chromium の実際のレンダリング結果で、DOM／アクセシビリティ情報は構造と
操作名を伝える別の観測である。画像はアイコン、重なり、視覚的配置の確認に使い、
操作は再解決した意味的対象を優先する。画像と構造は連続取得であり完全な同期は
保証できないので、`snapshot_id` と URL・時間の検査を保持する。ブラウザ以外の
macOS アプリでは HTML は得られず、AX 構造と将来のウィンドウ画像を組み合わせる。

1. macOS AX 操作範囲を広げる。観測で実際に列挙された二次 AX action を選択し、スクロール可能な要素と方向を指定できるようにする。対象とウィンドウの再確認、操作前拒否、結果不明の復旧を各操作で維持する。対象アプリのメニュー、ダイアログ、SwiftUI を試験する。
2. 画面画像と座標の補助経路を加える。Screen Recording 権限を別に検査し、正確なウィンドウ・倍率・座標系・画像 ID を返す。画像と同じ版の座標だけを許可し、移動・サイズ変更・別ウィンドウへの入力を拒否する。AX で操作できる対象は AX を優先する。
3. キー入力、ドラッグ、既存ブラウザタブを追加する。隔離ブラウザのフォーム観測は iframe と shadow DOM に広げ、どの frame の対象かを固定する。macOS の入力配送と既存タブ選択では、ユーザーの前面アプリやプロファイルに勝手に切り替わらないことを実機で確認し、不能なら明示的に拒否する。既存タブは所有者・ブラウザ・タブ ID・接続 grant を固定する。
4. Windows UI Automation と Linux AT-SPI を同じ観測／操作契約で実装する。macOS の成功を他 OS の合格としない。
5. 実機受け入れでは、構築した helper と配布物の一致、OS 権限、接続 grant、通常 Chat からのツール選択、各操作の事後観測、応答喪失後の operation ID 復旧を別々に記録する。少なくとも macOS のネイティブアプリ、再描画する Web ページ、曖昧な対象、別ウィンドウへの切替、権限なしの経路を試す。

実機で Calculator のサイドバー表示を `AXButton` とラベルで指定して実行し、
Computer Use 側で状態変化を確認した。操作直後の AX 再観測では一時的な
`AXWindows` エラーがあり、詳細ツリーは履歴の長いリストで128要素の上限に達した。
短い対象一覧と操作前の要素再探索をヘルパー側で幅優先にした結果、長い履歴を
表示した状態でも「サイドバーを非表示」を観測・実行でき、Computer Use 側で
元の表示に戻ったことを確認した。短い一覧でも `truncated=true` なので、
一覧にない要素の不在を証明するものではない。UI 変化直後の一時的な AX 失敗は
操作を再送する理由にせず、再観測で状態を確認する。意味的な操作は入力直前に
別の幅優先走査でウィンドウ内の一意性を確認する。走査が上限1024要素または
期限に達したら入力を拒否する。

汎用の GUI 押下・入力は送信や削除を起こし得るため、MCP の `destructiveHint`
は操作種別を示す保守的な注記として維持する。読取ツールには付けない。
この注記だけでユーザーの個々の操作意図や実行許可を推定しない。
