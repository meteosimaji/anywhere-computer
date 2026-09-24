# 変更履歴

公開パッケージと未公開の開発作業を分けて記載します。alpha版は正式stableではありません。
日付付きの実機検証記録はローカルの履歴資料として保管しています。

## 0.2.0a20 / Plugin 0.2.0-alpha.20 — unpublished candidate (2026-09-24)

- Finish HTTP generation operations as `preflight_failed` when the durable
  generation dispatch claim was never made. Keep the last failed preparation
  stage and HTTP status visible without replaying the operation. Claimed sends
  remain uncertain until history proves an outcome.
- Allow a bounded Cookie in the explicit HTTP session handoff so current
  observed Chat request headers can be bound to the session. Authorization,
  account, origin, and exact Cookie matching still apply. Independent all-HTTPX
  Chat generation remains unverified.

## 0.2.0a19 / Plugin 0.2.0-alpha.19 — unpublished candidate (2026-09-24)

- Accept current observed ordinary-Chat generation header names while preserving
  exact authorization and account binding through a verified Cookie when the
  request has no account header. Keep generation route headers off preparation
  requests. This resolves a handoff compatibility defect; independent HTTPX
  generation remains unverified.
- Add a names-only request-shape comparison utility and record the headless UI
  challenge that occurs despite successful authenticated HTTP GETs.

## 0.2.0a18 / Plugin 0.2.0-alpha.18 — unpublished candidate (2026-09-24)

- Add a Claude Code marketplace and local MCP Plugin configuration. The
  installed Claude Code CLI can reach GitHub and local sources, and its bundled
  computer and Subchat MCP servers connect. Claude cloud sessions and Chat/Cowork
  still require a separately configured HTTPS endpoint.
- Log only fixed response categories for an HTTP-only generation 401/403 to
  support diagnosis without retaining the response body or secrets. Independent
  HTTP-only generation remains unverified.

## 0.2.0a17 / Plugin 0.2.0-alpha.17 — unpublished candidate (2026-09-23)

- Preserve exact pre-a16 autostart definitions when inspecting and upgrading
  existing registrations. New registrations disable Python bytecode writes;
  existing receipts remain verifiable until an explicit upgrade.
- Include Sentinel finalize in the value-free transport probe so its request
  ordering and response status can be compared without recording credentials,
  token values or message bodies. This does not establish HTTP-only generation.

## 0.2.0a16 / Plugin 0.2.0-alpha.16 — unpublished candidate (2026-09-23)

- Prevent the portable Python runtime and its supported child processes from
  writing import bytecode into the verified payload. Discard build-time bytecode
  before sealing the manifest so repeated verification remains valid.

## 0.2.0a15 / Plugin 0.2.0-alpha.15 — unpublished candidate (2026-09-23)

- Redact malformed native GUI helper responses and accept only known helper
  error codes. Invalid helper output can no longer appear in a returned error.
- ChatGPT retried an a13 pre-dispatch Subchat operation with a14's readiness
  checks and recovered a saved final answer. This confirms browser-assisted
  Plugin sending, while independent HTTPX generation remains unverified.

## 0.2.0a14 / Plugin 0.2.0-alpha.14 — unpublished candidate (2026-09-23)

- Wait up to ten seconds for an idle ordinary Chat composer and observed model
  menu during browser-assisted Subchat preparation. Reject a visible draft,
  active generation or changed conversation immediately. Surface allowlisted
  preparation reasons without exposing provider error text.
- A ChatGPT-side a13 MCP send reached the Plugin but stopped in `prepared` before
  dispatch; the same saved operation passed preparation in a later direct probe.
  The exact original failing stage was not retained, so this readiness repair
  later passed a ChatGPT-side live send check using the same operation ID.

## 0.2.0a13 / Plugin 0.2.0-alpha.13 — unpublished candidate (2026-09-23)

- Add an explicit `browser-send` mode to the bundled Subchat Plugin. It exposes
  `subchat_send` through the existing dedicated Chrome transport while keeping
  the default HTTP observation mode read-only. The two modes use the same
  logged-in dedicated profile at different times; the send mode minimizes its
  Chrome window and reports `generation_transport=browser_prepared`. A live
  catalog probe observed Chrome briefly become the foreground app despite
  minimization, so this mode is not enabled by default.
- A live local MCP session sent a new ordinary Chat and a follow-up in the same
  conversation, then recovered both final answers. Independent HTTPX generation
  is still unverified and remains a release gate.

## 0.2.0a12 / Plugin 0.2.0-alpha.12 — unpublished candidate (2026-09-23)

- Send observed Sentinel protection headers only on the generation POST. A
  current successful UI control omitted them from Sentinel and conversation
  preparation requests. HTTP-only generation remains unverified.

## 0.2.0a11 / Plugin 0.2.0-alpha.11 — unpublished candidate (2026-09-23)

- Accept the current observed Chat requirements-token header in an explicit
  HTTP generation handoff without changing its value. HTTP-only generation
  remains unverified; the known 403 is not resolved by this compatibility fix.

## 0.2.0a10 / Plugin 0.2.0-alpha.10 — unpublished candidate (2026-09-23)

- Write the standard default namespaces in OOXML package metadata. The prior
  DOCX could be read internally but LibreOffice rejected it. Verify generated
  DOCX with an isolated LibreOffice PDF conversion when available.
- Ordinary Chat HTTP-only generation remains unverified.

## 0.2.0a9 / Plugin 0.2.0-alpha.9 — unpublished candidate (2026-09-23)

- Keep Chrome-profile cookies in HTTPX's scoped, in-memory cookie jar so
  `Set-Cookie` updates reach later requests. Check a generation handoff against
  the current cookie before sending; explicit handoffs keep their behavior.
- Bound preparation JSON while receiving it and reject compressed generation
  streams before SSE decoding. HTTP-only provider generation remains unverified.

## 0.2.0a8 / Plugin 0.2.0-alpha.8 — unpublished candidate (2026-09-23)

- Reject compressed sandbox download responses and inspect raw response chunks,
  preserving the byte limit before any HTTP content decoding could expand them.

## 0.2.0a7 / Plugin 0.2.0-alpha.7 — unpublished candidate (2026-09-23)

- Bound sandbox download metadata while receiving it, so an oversized response
  cannot be buffered in full before validation. Bound file chunks before appending.
- a6 passed all five Quality CI jobs, including the Windows full suite and
  relocated portable verification. ChatGPT accepted status/capabilities, but
  blocked Subchat catalog/list before dispatch; Codex completed both tools.

## 0.2.0a6 / Plugin 0.2.0-alpha.6 — unpublished candidate (2026-09-23)

- A temporary Chat catalog tab close timeout no longer replaces the completed
  read or its original error; one bounded cleanup retry is logged if needed.
- Expose a saved final-answer sandbox file as a bounded, account-checked,
  read-only MCP download. Cross-Chat Library upload remains unsupported.
- Windows CI exercised the ConPTY resize acknowledgement; macOS CI exposed the
  tab-close race being corrected in this candidate.

## 0.2.0a5 / Plugin 0.2.0-alpha.5 — unpublished candidate (2026-09-23)

- Windows ConPTY resize now waits for a worker acknowledgement before reporting
  the new dimensions.
- Added an explicit, target-scoped Cua GUI adapter and removed unverified provider
  images from its observation output.
- Improved read-only HTTP Subchat catalog defaults and unknown-operation errors;
  refined same-build diagnostic evidence without changing authorization.
- Refuse dirty Plugin release bundles by default, pin CI action revisions, and
  provide private vulnerability reporting guidance.
- Headless Chrome-profile HTTP authentication and model catalog returned 200;
  this candidate has not yet passed new/follow-up HTTP generation acceptance.

## 0.2.0a4 / Plugin 0.2.0-alpha.4 — unpublished candidate (2026-09-23)

- Dedicated Chrome login rejects a different selected account immediately after
  the HTTP authentication GET, before requesting the model catalog.
- Explicit HTTP generation handoffs accept a successful observed request that omits
  `openai-sentinel-chat-requirements-prepare-token`. Other required headers and
  account/origin checks remain enforced; the missing header is never synthesized.
- Simplified duplicated HTTP error recording without changing retry semantics.
  Auth and catalog HTTP 200 were rechecked with the logged-in dedicated profile;
  new HTTPX generation for this candidate remains unverified.

## 0.1.0b1 / Plugin 0.1.0-beta.1 — published prerelease (2026-09-14)

- Common local Skills and hash-checked resource reads without Codex; persistent
  collection configuration through `anywhere skills-configure`.
- Windows startup recovery when the owner pipe is unavailable, while preserving
  a live unresponsive agent and its work.
- Retained MCP/HTTP clients prepare the selected agent before catalog lookup;
  authorization is rechecked after startup and dispatched writes are not replayed.
- Typed GUI calls preserve provider errors and native images. Incomplete or
  ambiguous observations cannot authorize input.
- These changes are integrated through PR #36. They are not included in the
  published alpha 9 ZIP. Beta 1 freezes existing features; remaining roadmap
  features are explicitly outside its supported scope.

## 0.1.0a9 / Plugin 0.1.0-alpha.9 — published prerelease (2026-09-13)

- ChatGPTのHTTP入口に、明示的な端末指定のdevices_list / devices_tools / devices_callを追加。
  共通エンジン構成ではCodexと端末登録を共有。既存の制限付き認可は自動で拡張しない。
- HTTP認可ごとに転送操作IDを分離。同じ端末・同じ認可で結果を回収し、応答喪失時に再送しない。
- 応答喪失後も既知のHTTPセッションIDを保持し、結果照会や終了で再利用する。
- Windows試験のシェル特殊文字を修正し、全件試験を2並列化。重点試験を先に実行する。

## 0.1.0a8 / Plugin 0.1.0-alpha.8 — 開発版

- OSによるMCP起動拒否でセッション枠が残る不具合を修正。
- 端末の同時起動上限を保護し、入力開始後の送信・観測例外をunknownとして保存。
- 端末コマンドより長寿命の所有プロセスを導入。POSIXの同じプロセスグループ、WindowsのJob内の
  子が残る間はセッションと更新ブロッカーを保持し、終了要求時にまとめて停止。
- 直接MCPの一覧に要約・全文検索・名前指定を追加。検索はページ単位、続きcursorを保持。
- Peekaboo向けgui_observe/gui_click/gui_type/gui_keyを追加。観測の所有者・期限・一回使用を検証し、
  限定した座標情報を検証して保持。座標クリックは未提供。追加モデル推論は行わない。

## 0.1.0a7 / Plugin 0.1.0-alpha.7 — 開発版

- `http-add-tools`：認証情報を保持したHTTPツール追加。旧全件認可のみ拡張し、制限付き・失効・期限切れは保持。
- HTTP公開対象54ツールの機械受け入れ試験、Peekabooによる電卓の連続操作・回収を行う明示的実機試験を追加。
- Computer Useの送信元認証拒否を、秘密本文を保存せず固定分類で診断。

- GitHub stable候補の取得、出所証明・ハッシュ・OS／CPU・API／台帳互換性の検査。
- 隔離展開、同梱Pythonの起動前検査、候補の保存、待機・中断後の再開。
- 1回の更新を実行する `anywhere update` と、開発中の常駐監視への接続。
  自動更新は既定で無効。利用者が明示的に有効化する。正式stableによる実機適用は未確認。
- Codexを経由しない直接MCP接続の内部実装。試験サーバーの状態保持と、
  Playwright MCPを通した隔離ブラウザーの実操作を確認。
- 開発版の `mcp_session_open/status/close`、`mcp_tools`、`mcp_call` を登録。
  接続元別のセッション、同時起動上限、操作ID再送時の結果回収、更新待機を検証。
  カタログのページ取得と、実行中の呼び出しを保護するアイドル終了を追加。
  画像結果のMCP中継と台帳回収を検証。一般向け配布は未完了。
- README、配布・運用・更新ガイドの整理。

## 0.1.0a6 / Plugin 0.1.0-alpha.6 — 2026-09-12

- 明示更新で選択した実行ファイルとruntime IDを保存し、通常接続時の起動先に使用。
- 切替前に中断記録を保存し、同じ候補から明示的に再開可能にした。
- 不明・破損・消失した選択先へ、古い同梱版で暗黙に戻らないようにした。
- macOSの共通エンジンと常駐版へ適用し、既存HTTP認証による接続と旧操作結果の回収を確認。

## 0.1.0a5 / Plugin 0.1.0-alpha.5 — 2026-09-12

- エンジン移行で、SHA-256名の管理対象バックアップと無関係な手動アーカイブを区別。
- 既存のMCPセッションを維持したエンジン更新と、更新前の結果回収を確認。
- macOS実環境でChatとCodexの共通エンジン利用を確認。

## 0.1.0a4 / Plugin 0.1.0-alpha.4 — 2026-09-12

- HTTP入口から認証済みローカル通信へ転送する共通エンジン経路を追加。
- 保存先の選択、台帳・転送・バックアップのオフライン移行、中断記録からの続行を追加。
- 互換APIのエンジンを、クライアントのコードハッシュが異なるだけでは置換しないよう変更。

## 0.1.0a3 / Plugin 0.1.0-alpha.3 — 2026-09-12

- macOSの常駐登録解除で、受付から実際の解除完了までの遅延を扱うよう修正。
- 台帳移行、遅れて完了した画像の回収、更新時の停止境界の競合などの監査対策を反映。
- macOS常駐版の更新と、既存HTTP接続からの再接続・旧操作結果の回収を確認。

## CI・配布基盤

公開commit `b6e0561`で、mainのQuality CIが生成した配布ZIPに出所証明を付け、
同じworkflow・commit・refの証明として検証する処理を追加しました。
run 34693834133はmacOS・Windows・Ubuntuすべて成功しました。
これは正式stableの公開や、未公開変更のCI成功を意味しません。
