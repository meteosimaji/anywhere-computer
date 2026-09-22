# subchat完全HTTP化：監査成果・実測・実装方針（2026-09-22）

対象ソース：[`3b53ef2805032b1169b51e1e46f461d8bedd6593`](https://github.com/meteosimaji/anywhere-computer/tree/3b53ef2805032b1169b51e1e46f461d8bedd6593)。本書は2026年9月22日の一連の調査結果をまとめた記録であり、HTTP生成機能の完成報告ではない。現在の操作契約は[HTTP recovery guide](SUBCHAT-HTTP-RESEARCH.md)と[Subchat guide](SUBCHAT-PROBE.md)を正本とする。

[保存した証拠・再現手順](research/2026-09-22-subchat-http/README.md)／[機械可読の結果](research/2026-09-22-subchat-http/evidence.json)／[原本・公開ファイルのハッシュ](research/2026-09-22-subchat-http/manifest.json)

## 1. 結論と今回の目的

通常Chatのブラウザー経由の生成POSTと、独立HTTPによる回答回収には既存の成功記録がある。しかし、認証後の生成準備・新規作成・追送・受信・回収を独立したHTTPクライアントだけで完結させた成功例は、この調査では得られていない。現行`--http-only`は読み取り専用で、新規生成を`http_generation_unavailable`として拒否する。

これは「技術的に不可能」という結論ではない。現在の障害は、正規の認証セッションを取得・更新する方法と、通常経路で観測された生成準備を独立senderで成立させる契約が未確定なことである。本文JSONの検査を通過しただけでは、この二つを満たさない。

非干渉の対象は、subchatを作り、送受信・回収する通信経路である。ユーザーが明示したとおり、子Chatが依頼された作業のためにChromeやGUIを操作することは禁止対象に含めない。子ごとのsandbox、任意shellの制限、GUIの全面禁止を、このHTTP化の必須条件へ追加しない。最初の設計案にあった「子の全作業まで非干渉を保証する」という範囲拡大は、本書では採用しない。

通信経路は、自動的なChrome起動、tab操作、前面化、入力、対話的ログインへのfallbackを行わない。初回の認証操作を明示的に行う段階と、その後のHTTP実行区間を区別する。生成のたびにブラウザー準備を必要とする構成や、headless browserの代用は、完全HTTP化の達成とは扱わない。

## 2. GitHub反映と基準版

調査初期は、GitHub mainが`e2100eb`、ローカルがPR #152のHEADである`3b53ef2`で、ローカル側が2コミット先行していた。Git管理対象457 blobの内容・属性の照合では不一致がなく、未コミット・未追跡変更もなかった。Git管理外の設定・依存関係・認証状態の一致は含まない。

その後、ユーザーの依頼により、2026年9月22日18:32:12（日本時間）にmainを通常のfast-forwardで`3b53ef2`へpushした。force pushは使わず、[PR #152](https://github.com/meteosimaji/anywhere-computer/pull/152)も統合済みになった。本書作成開始時の再照合でも、ローカルHEADとGitHub mainは同じだった。今回の文書追加commitは、その基準版に研究記録を追加するものとなる。

`3b53ef2`に含まれる変更は、監視解除後の`queued`回収枠解放と、そのテスト・文書・バンドル更新である。HTTP生成senderの追加ではない。

### 導入済みエンジンとの区別

| 比較対象 | 9月22日に確認した実装指紋 |
| --- | --- |
| ローカル`3b53ef2`のPython／JavaScriptソース | `f16297c94505d82674a34bcd2c939792600d6720607dfe3d5790f473a9620025` |
| 同版の0.2.0a1 bundled wheel | 同じ`f16297c94505…` |
| 接続中の常駐エンジン | `a82ce393e115c5b1025a0c8777823d74357ee120f660892d27aca78d71084c53` |

同じ`0.2.0a1`表示だけでは実装の一致を判断できない。指紋の計算対象はpackage内の`.py`／`.js`であり、native helperや依存ライブラリ全部の保証ではない。この一連の調査で常駐エンジンの更新・再起動は行っていない。文書追加も実装指紋を変える製品修正ではない。

## 3. 検証結果の整理

以下は調査中に実行した結果である。文書を公開したことによって再実行済みになるわけではない。テスト集合は重複し得るため、件数を足して総検証数にしない。

| 検証対象 | 結果 | 証拠の範囲 |
| --- | --- | --- |
| 初期main `e2100eb`の既存関連試験 | 150 passed | 選択した既存試験 |
| `3b53ef2`の既存関連試験 | 151 passed | 選択した既存試験 |
| `e2100eb`への追加監査試験 | 5 failed | 合成provider、SQLite、stdio、GUI不整合応答 |
| `3b53ef2`への同じ追加監査試験 | 1 passed、4 failed | PR #152で直るケースと残存ケースを区別 |
| `3b53ef2`のローカル標準全体試験 | 1,652 passed、19 skipped、894.47秒、exit 0 | 実Chat生成の受入ではない |
| 同版のRuff・mypy・README参照検査・build | 成功。mypyは115 source files | sdistとsdist由来wheelも作成 |
| main push後のQuality CI | 5ジョブすべて成功 | [run 35710942533](https://github.com/meteosimaji/anywhere-computer/actions/runs/35710942533) |
| HTTP作成診断のオフライン試験 | 8 passed、exit 0 | 診断の合成データ試験。実認証・生成ではない |
| push後のtransport監査再試験 | 2 failed、3.49秒 | 回収8枠と外側idleの問題が引き続き再現 |
| 要求検査器の追加境界試験 | 16ケース、想定判定と全一致、exit 0 | ソケット接続試行0、生成POST 0 |

標準全体試験と基準CIが成功しても、追加監査の失敗や未実装が解消したわけではない。[全体試験ログ](research/2026-09-22-subchat-http/full-tests.log)、[基準CI結果](research/2026-09-22-subchat-http/ci-baseline.json)、[監査の観測値](research/2026-09-22-subchat-http/evidence.json)を分けて保存している。

前回PR CIのWindows起動待ち失敗では、`test_local_watch_does_not_replace_or_stop_live_engine`が5秒の確認期限を超え、初期化段階が約13.188秒だった。その後のmain CI成功だけで、この遅延の原因を究明・修正したとは扱わない。

## 4. 再現できた問題と修正済み範囲

### A1：内側watchと外側Direct MCPの寿命不一致

`direct_mcp_sessions.py`のidle期限は標準300秒で、子プロセス内のqueue watchが動いていても、外側の`last_used`は更新されない。実DirectMCPSessionsとstdio子プロセス、合成providerで監視開始を確認し、注入時計を301秒へ進めると、外側は`expired`、cleanup確認済み、子は`queued`、送信は親の1回のみになった。

これは301秒を実時間で待った実Chat試験ではない。文書化されたidle終了と自律監視要件の不整合であり、追送の進行が止まる。無期限idleに変えるのではなく、既存engineに作業の所有者・有効期限・停止理由を持たせる設計が必要である。[固定版ソース](https://github.com/meteosimaji/anywhere-computer/blob/3b53ef2805032b1169b51e1e46f461d8bedd6593/src/anywhere_computer/direct_mcp_sessions.py)

### A2：終了済みpending回収による8枠占有

短い`subchat_wait`が先に終わった後、providerが「回答なし」を返すと、taskは終了しても`submitted`の結果を持ったまま回収枠に残る。8件すべてが終了済みなのに、第9の別操作の回収が`RuntimeError`で失敗した。

元の操作IDを回収すれば枠が解放される経路はあるので、不可逆デッドロックや無制限のメモリーリークとは呼ばない。実行中容量と未受領結果・例外を分け、正常pendingで実行枠を塞がず、遅い例外を捨てないよう修正する。[固定版ソース](https://github.com/meteosimaji/anywhere-computer/blob/3b53ef2805032b1169b51e1e46f461d8bedd6593/src/anywhere_computer/subchat_mcp.py#L194-L246)

### A3：watch解除後のqueued回収残留はPR #152で修正済み

watchを解除した後に終了する`queued`回収が枠に残るケースは、初期mainで失敗し、`3b53ef2`で成功した。この修正を未修正として二重計上しない。A2は`submitted`のケースなので、PR #152だけでは直らない。

### G1：GUI側の互換性・対象照合は別件として残す

合成providerが「seeはwindow指定に対応するがtypeはtextだけ」というschemaを返しても、wrapperは入力dispatchへ進んだ。同じアプリで要求window 42に対して43を示す不整合応答でも、要求側の42を返して`action_ready=true`になった。

これはwrapperの防御境界の試験であり、実Peekabooで誤入力した証拠ではない。注入したwindowメタデータも正式なprovider schemaだとは主張しない。入力側のsnapshot契約・応答の対象一致・定数の`focus_may_change_externally=false`を見直す余地がある。[固定版ソース](https://github.com/meteosimaji/anywhere-computer/blob/3b53ef2805032b1169b51e1e46f461d8bedd6593/src/anywhere_computer/gui_mcp.py)

native AXの値変更・AXPressには限定的な実機成功の既存報告があるが、保存結果・任意アプリ・Windows UIAの全面受入とは別である。[GUIの既存報告](GUI-RELIABILITY-2026-09-14.md)を正本とし、この別件をHTTP送信の完了条件へ混ぜない。

## 5. HTTP送信要求について分かっていること

### 通常経路の観測と独立senderの成功は別

現行の通常経路では`POST /backend-api/f/conversation`とSSEによる会話ID候補の取得が記録されている。しかしブラウザーが認証・生成準備・本文構築を担当する。POSTの検査・転送をしていることは、独立したHTTP senderであることを意味しない。[固定版の生成実装](https://github.com/meteosimaji/anywhere-computer/blob/3b53ef2805032b1169b51e1e46f461d8bedd6593/src/anywhere_computer/subchat_browser/backend.py#L246-L360)

以下は「観測またはローカルで検査される項目」であり、providerの正式な最小必須仕様ではない。

| 項目 | 確認した役割 | 未確定事項 |
| --- | --- | --- |
| `Authorization` | 成立済み通常Chatセッションの認証。現行readerはBearer形式を受け取る | 独立ログイン・更新・生成受理までの完全契約 |
| `chatgpt-account-id` | readerと送信前checkpointのaccount照合 | 全provider経路の必須性をローカル検査から一般化しない |
| JSON本文／SSE応答のメディア型 | 送受信する表現 | これだけで認証や準備が成立するわけではない |
| `oai-language` | 言語指定 | 生成許可の根拠ではない |
| `action: next`、`messages` | 現行controllerはuser入力1件の形を検査 | providerの全操作・全メッセージ形式ではない |
| `messages[0].id`、`author.role` | 入力IDとuserロールの照合 | 同一ID再送でのprovider冪等性は未証明 |
| `content_type: text`、`parts`、`metadata` | 本文文字列一件とmetadata辞書の検査 | 空metadataで生成条件が足りるかは未証明 |
| `model`、`thinking_effort` | 観測済み`http_selection`に対する一致 | UI表示名をslugやeffort値へ推測変換しない |
| `conversation_id`、`parent_message_id` | 新規と追送で異なる既存観測 | IDの発行元、親・branchの正しい指定、全版での省略規則 |
| `client_prepare_state`、準備・検証用header | 正常経路に存在したという記録 | 完全schema、発行元、有効期間、個々の必須性 |
| 添付・plugin参照 | 既アップロードファイルと観測済みplugin指定 | ローカルパスや名称だけで代用しない |

初回正常送信で`conversation_id`／`parent_message_id`が省略され、追送で両方が含まれた記録がある。任意のUUIDを両方へ入れる実装にも、全新規送信で永久に不要とする実装にも根拠はない。`operation_id`、入力ID、会話ID、要求／turnの相関情報を別々に扱う。[固定版の観測記録](https://github.com/meteosimaji/anywhere-computer/blob/3b53ef2805032b1169b51e1e46f461d8bedd6593/docs/SUBCHAT-PROBE.md#L1883-L1906)

### 16ケースの要求検査器試験

`generation_input()`は、`http_selection`なしのmodel省略、追送の親ID欠落・未検証の親ID、任意の合成`client_prepare_state`を通す。一方、指定model／effort、本文、roleの不一致や既存resourceの混入を拒否する。

この関数はブラウザー作成済みrequestの取り違え防止であって、認証・分岐・生成準備を含むrequest全体の構築器ではない。合格をサーバー受理の証拠にしてはいけない。すべての他の層も未検証の親IDを通すと証明したものでもない。[16ケースの結果](research/2026-09-22-subchat-http/request-validator-cases.json)、[再現コード](research/2026-09-22-subchat-http/probes/contract_boundary_probe.py)、[検査器の固定版](https://github.com/meteosimaji/anywhere-computer/blob/3b53ef2805032b1169b51e1e46f461d8bedd6593/src/anywhere_computer/subchat_browser/request_content.py)

### 認証と生成準備の障害を分ける

9月21日の既存記録には、独立Playwright HTTP clientでcatalog/history GETが成功しても、構築した生成POSTはJSONのunusual-activity 403になったとある。ブラウザーとCookieを共有するHTTP clientでも同様だった。一方、httpxのcatalog GETではHTMLのchallenge 403という別の結果が記録されている。

正常経路には会話初期化・生成準備が観測されているが、どのfieldが拒否の原因かは未確定である。Cookie共有、header追加、HTTPライブラリ変更だけで解決するとは結論しない。既存の保護値のコピー・偽造・使い回しを、独立した正規の生成準備の代わりにしない。[固定版の診断記録](https://github.com/meteosimaji/anywhere-computer/blob/3b53ef2805032b1169b51e1e46f461d8bedd6593/docs/SUBCHAT-PROBE.md#L1731-L1823)

9月22日18:29の別の実機診断は、非対話的なKeychainアクセスがOSStatus `-25293`で拒否され、HTTP前で停止した。生成POST 0、browser起動関数0、page操作0、exit 2だった。これはproviderの403を再観測した結果ではない。18:51の追加調査でも新たな認証済みsessionがなく、Keychain再試行・生成POSTは実施していない。[認証段階での停止記録](research/2026-09-22-subchat-http/http-creation-result.json)

ここでの0回は各診断の範囲に限る。標準全体試験の制御browser fixtureやGitHubとの通信まで0だったという意味ではない。OS拒否の原因をパスワード誤入力・特定設定と断定しない。公開クライアント資産の追加取得も実行前のtool拒否により未実施で、完全な現行準備schemaは取得できていない。

## 6. 実装に直結する追加実測

### 6.1 途中受信できるHTTP transportが必要

localhostのHTTP/1.1 chunked SSEで、先頭の架空会話IDイベントを先に送り、末尾を保留した。Playwright 1.58.0の通常`APIRequestContext.post`は1秒の観測窓内にAPIResponseを返さなかった。一方、HTTPX 0.28.1の`AsyncClient.stream`は末尾解放前に先頭イベントを取得した。解放後は両方とも完全な本文が一致し、POST受信数は各1回、browser起動関数0、exit 0だった。[実測結果](research/2026-09-22-subchat-http/streaming-result.json)

速度ベンチマークでも、全版の一般的な性能保証でも、HTTPXでChatGPTが受理するという証明でもない。動いているGET readerを一括置換せず、逐次受信できるsenderの機能とproviderでの受理を別々に試験する。

現在の`subchat_stream.js`が閉じるのは応答のcloneで、ページ側の元応答は別に継続する。独立senderが持つ元streamを、会話IDが得られた直後や親のwait期限切れで閉じてよいとは限らない。workerが元streamの寿命を所有する。[固定版observer](https://github.com/meteosimaji/anywhere-computer/blob/3b53ef2805032b1169b51e1e46f461d8bedd6593/src/anywhere_computer/subchat_browser/subchat_stream.js)

historyの4 MiB検査は`response.body()`取得後の検査なので、受信時メモリー使用量の保証ではない。新しいstreamでは、UTF-8／改行のchunk跨ぎ、複数data行、単一event・buffer・デコード後サイズの上限、無通信、未完了EOFを個別に試験する。一般SSEの再接続機構がこのproviderでも使えるとは推測しない。

### 6.2 workerごとのLedger初期化は既存稼働状態を変える

合成の一般operationを`running`で保存し、同じ隔離directoryに二つ目の`Ledger(...)`を開くと、最初の接続からも`unknown`になった。`Ledger._initialize()`の再起動回復処理によるもので、実ユーザーの操作を壊した再現ではない。[実測](research/2026-09-22-subchat-http/ledger-lifecycle-result.json)、[固定版store](https://github.com/meteosimaji/anywhere-computer/blob/3b53ef2805032b1169b51e1e46f461d8bedd6593/src/anywhere_computer/state.py)

同一engine内では既存connectionを利用する。別processにするなら、限定RPC経由の台帳更新、または単なるDB接続とengine再起動回復の分離が必要である。新workerごとに稼働中engine全体の回復処理を走らせない。

## 7. 次の実装順と受入条件

以下の新しい型名・file名は提案であり、現行tool/APIではない。別の汎用タスク基盤や第二の台帳を作ること自体を目的にせず、既存`Subchats`、store、history projector、engineを利用する。

| 順序 | 対象 | 実装・受入の要点 |
| --- | --- | --- |
| 1 | 通信modeと能力表示 | browser-assisted、HTTP-read-only、独立HTTP-generationを区別。UI fallbackなし。未実装の静かな拒否は暫定防御であって完成ではない |
| 2 | 認証・正規の生成準備 | 初回bootstrapと以後の非対話的運用を分離。正常経路のfield名・型・出所・依存関係を記録し、秘密を成果物へ含めない |
| 3 | `subchat_http_send.py`等の送信計画 | new/follow-upを分け、観測済みmodel／effortと本文・resource・account・親branchを固定する。UIスライダー不要の構成 |
| 4 | `subchat_state.py`の原子的送信予約 | operation、input ID、account、計画digestをPOST前に保存。保存失敗で送らない。既存更新器の外側で単にtransactionを入れ子にしない |
| 5 | streamと履歴確定 | 候補IDを早期保存。200、EOF、`[DONE]`だけで完了としない。既存の本文・resource・相関・一意final検査を維持 |
| 6 | 回収容量とengine所有worker | A1/A2を解消。呼出しのwait期限と生成寿命を分離し、late error・停止理由を保存。別会話を大域lockで塞がない |
| 7 | 導入版での確認 | source・wheel・runtime・toolを照合し、通常Chatから導入済みAnywhereを通したnew/follow-up/再接続を検証 |

遠隔の初期化POSTが会話等の状態を作るなら、副作用のないローカルprepareと同一視しない。各段階の予約・結果・不明状態を残す。`sending`以後の切断や拒否を、普通の`prepared`へ巻き戻さない。

同じoperation IDによるローカル重複防止とprovider側のexactly-onceは別である。同じUUIDのPOSTを再送してよいとは保証されない。会話ID保存前のcrashは、providerの冪等性・receipt検索契約が確立しない限り、明示的な照合待ちを残す。

完了判定では、実際に省略されることを確認済みの`is_complete`と`finish_details`を再び常時必須へ戻さない。存在するfalse/null/不明finishや明示的なinterruptedは成功にしない。異なるrequest IDのasync finalを受理する既存の厳密な相関条件も維持する。

長時間生成を単にengineのinflightへ追加すると、`Engine.close()`の待機にも影響する。受付toolと持続workerを分け、更新時の新規受付停止、上限付きdrain、結果不明の保持を確認する。workerやwatchの終了をproviderの生成停止と表示しない。現在のcancelはローカル未送信の取消であり、provider Stopやnative steerではない。

### 最初の実サービス受入

最初は、添付・plugin・検索指定・steerなしの新規通常Chat一件と、その会話への一回の追送に絞る。認証後、senderのbrowserアクセスを禁止し、現在のcatalogから選んだaccount・model／effort・短い合成promptを固定する。各生成要求を一度だけ送り、保存したinput、streamの候補、HTTP履歴のinputと一意なfinalを照合する。

「初回認証後に独立HTTP生成できる」と「cold login・期限切れ更新まで自立している」は別の段階である。認証だけ成立しても生成ごとにbrowser準備が必要なら未達。API側のconversationやCodex／Workを通常Chatの代わりにして成功扱いしない。

非干渉試験は送受信経路のcold/warm、new/follow-up、成功/拒否/認証待ちを対象とする。全画面解除・前面化が途中で起きてから戻る動作は不合格。子自身のGUI作業は今回の合否対象外とし、通信経路による変化と区別する。

## 8. 公開したもの・していないもの

公開するのは本書、選別した非秘密の結果、原本SHA-256と必要なパス置換の記録、合成provider／localhostで再現する4本の診断コードである。追試は[証拠directoryの手順](research/2026-09-22-subchat-http/README.md)に従い、固定版・別の一時directoryで実行する。追加監査の失敗を意図的に無視して標準suite成功へ混ぜない。

認証取得・復号コード、Cookie、Bearer、保護token、browser profile、実会話本文、個人directoryを含む生tracebackは公開しない。基準の8件の診断試験については結果ログを保存するが、その認証取得コードはこの文書追加には含めない。成果の公開は、新たな認証操作・実Chat生成・権限変更の承認としては扱わない。

9月21日の実サービス実験、9月22日の合成・localhost実測、9月22日の認証前段で停止した診断、文書の公開は別の行為である。今回の記録から通常Chatの独立HTTP新規作成が完了したとは主張しない。

## 9. 文書公開前の追試

保存予定の4本のprobeを別の一時directoryへコピーして実行し、要求検査16ケース、localhost stream、隔離Ledgerの観測値が保存済み結果と一致することを確認した。追加監査は1 passed／4 failedを再確認し、未修正の期待違反を成功に書き換えていない。package一致とruntime identityの既存11試験は成功した。

文書の相対リンク、JSON／Python構文、コピー・投影した11ファイルのmanifestハッシュ、README生成部分との整合、差分の空白検査を確認した。公開ファイルについて、個人directoryと代表的な秘密値形式の混入も確認した。全体suiteや実Chat生成はこの公開作業では再実行していない。[公開前の検証記録](research/2026-09-22-subchat-http/publication-validation.json)
