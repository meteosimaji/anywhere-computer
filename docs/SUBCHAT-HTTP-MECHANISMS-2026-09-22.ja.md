# 通常Chatの独立HTTP化：公開実装の仕組み、再現試験、実装計画

調査日：2026年9月22日。Anywhereの基準実装は `61d38e091ac4d5d1a179c6cee759255d9ceb9d8b`（直前の研究文書更新を含む）。本書は公開実装の追加調査と隔離試験であり、通常Chatの独立HTTP生成の受入成功報告ではない。

## 1. 結論と対象範囲

直接HTTPで通常Chatの生成を行うコードは存在する。しかし、公開コードを取得できたこと、作者が特定日に成功を報告したこと、現在の利用者の認証で再現できること、Anywhereの永続回収まで正しく統合できることは別である。

今回、5プロジェクトのHEADを新しく照合し、関連31ファイルを取得した。取得した全行・全機能を監査したという意味ではなく、主要な送信・回収・Codex連携の経路と、後述の試験対象を精査した。外部依存の未公開部分やサービス側の判定実装まで分かったとはしない。

合成データによる上流関数の特性試験12件と、新規SSE受信参照実装の受入試験21件を実行した。前者では切り詰め、形式の読み落とし、キャンセル後の枠消失などを再現した。後者ではlocalhostへの実HTTP通信も行った。対ChatGPTの生成POST、認証情報取得、検証・保護機構の迂回は行っていない。

非干渉の対象はsubchatの作成・送信・受信・回収である。子が依頼された作業の一環としてChromeやGUIを操作することは対象外であり、それを禁止する権限基盤をHTTP化の前提にしない。

詳細な数値・ソースのハッシュ・再実行手順は [証拠フォルダー](research/2026-09-22-http-mechanisms/README.md) を参照。前回までの国内環境・台帳監査は [統合監査](SUBCHAT-HTTP-AUDIT-2026-09-22.ja.md) に残す。

## 2. 同じ「API対応」でも3つの異なる仕組み

| 方式 | 呼び出しから生成まで | Anywhereへの意味 |
|---|---|---|
| Codexの外側モデルをChatGPT Webに置換 | Codex Responses要求 → ローカル変換器 → Webモデル → ツール・回答をCodexイベントへ戻す | 文脈・出力・ツール変換まで必要。送信自体がブラウザーとは限らず、実際の上流を確認する |
| MCPツールとして通常Chatへ委譲 | Codex等の主モデル → MCPのchat → HTTP生成 → テキスト返却 | subchatに近いが、会話ID・入力ID・操作の永続性が省略されやすい |
| Codex専用通信を互換APIとして提供 | 任意クライアント → Responses変換 → Codex用upstream | 同じchatgpt.comドメインでも通常Chatの新規会話作成とは別 |

localhostに `/v1/responses` を設けただけでは、ChatGPTまでの生成がHTTP-onlyだとは証明できない。外側のAPI名ではなく、最終送信先、ブラウザー操作、会話IDの発行・保存・追送を確認する。

## 3. ChatGPT Web for Codex の経路

対象：`miuuyy/codex-chatgpt-web`、固定HEAD `eaf4f09ae92d4dc4429fa597b0861663138f08f8`。

`docs/architecture.md` と `mcp-server.ts`、`turn-broker.ts` の対応箇所を読むと、主経路は次のように分かれる。

CodexはローカルResponsesサーバーへ要求を出す。Web用のモデル行だけが専用adapterへ分岐し、Codexの文脈はWebモデルが読める入力へ変換される。生成はlauncherが所有するElectronのタブで行われる。タブはtask・model・effort・compaction世代に束縛され、同じ世代の順次メッセージは表面を再利用する。別途Chromeをインストールしないことと、ブラウザー処理が存在しないことは違う。

browser-onlyモードはローカルツールを提供しない。fullモードは、ChatGPTのcustom connectorからトンネル経由でMCPへ到達し、その呼び出しをouter Codex turnのbrokerへ中継する。brokerにはturn token、binding、call ID、活動中の呼び出し、退役済みtokenなどの区別がある。ツール結果は同じChatGPT応答へ戻され、最終回答等がCodex側のイベントへ変換される。モデルが出したJSONを適当にshellで実行するだけの仕組みではない。

`turn-broker.ts` は環境のcwd・roots・writableRoots・sandboxPolicy・tool一覧を検証する。これは外側Codexの実行環境と呼び出し先を取り違えないための機構であり、通常ChatのHTTP認証そのものではない。完了fenceや活動revisionも、遅れて到着するツール呼び出しと完了処理の競合を扱うために存在する。

設計書にはwait_agentを10秒で返すという箇所がある一方、取得した `mcp-server.ts` の `CHATGPT_WEB_AGENT_WAIT_POLL_MS` は30,000ms、MCP呼び出し上限は90,000msだった。同じ固定HEAD内でも説明と定数に差がある。数値は現行ソースを基準に確認し、文書だけをコピーしない。これはソース比較であり、実ランタイムで計測した期限ではない。

`retry-policy.ts` にはプロセス内の再試行予算がある。上限付きであることだけでは、送信済み要求を再送してよい根拠にならない。Anywhereは再試行の対象を未送信準備・観測・生成要求に分け、自身のunknown再送禁止を維持する。

この方式から参考にできるのは、外側turnとWeb turnの対応、ツール呼び出しの相関、compaction世代、終了時のdrainである。Electronを内蔵しても、今回の独立HTTPという達成条件にはならない。[M]

## 4. gpt2agent の経路と、そのまま採用できない部分

対象：`robotlearning123/gpt2agent`、HEAD `38a78fe8affb7d23955f01b45a98924b3e7714ab`。

`server.py` のMCP `chat` は、manual指定なら入力を返すだけ、browser指定なら別のChrome経路、通常は `ConversationClient.complete()` を呼ぶ。下位のSSEクライアントには要求準備、本文構築、直接HTTP POST、逐次イベント解釈、必要な追加観測の経路がある。したがって、HTTP APIという名前だけで内部がすべてDOM操作という実装ではない。

ただし通常chatツールも `temporary=True` が既定で、主な返り値は文字列である。継続subchatとして利用するには、永続会話の扱い、入力ID・会話ID・回答IDの返却、追送先、再起動後の回収を明示的に追加する必要がある。

また、`browser=False` だけで「絶対にブラウザーを使わない」とはならない。`server.py` は直接経路のchallenge例外時に、設定でbrowserが有効なら `_chat_via_browser` を呼ぶ。usage-limit例外時には、設定された別モデルへのfallbackもある。切替自体は結果に注記される設計だが、今回の固定HTTP・固定モデルの契約には無条件で引き継げない。

作者は9月17日のHTTP復旧を報告している。今回読んだ `artifacts/verify/sentinel-restore-20260917/receipts.md` は4行で、応答マーカー等の成功要約である。対応する会話ID・入力ID・HTTP応答・独立追試が一式含まれた証拠ではない。READMEが明記する未同梱の外部処理もあるため、作者報告をこちらの再現成功へ昇格させない。

### 履歴は一覧表示用と、完了確定用を分ける

`get_conversation` はsingularの `/backend-api/conversation/{id}` のgraph形式を扱う。`current_node` から親を辿り、選択中のbranchを返す。一方、current_nodeがない場合は全ノードを時刻順に並べるfallbackとなる。Anywhereの既存flat形式のhistory projectorと同じschemaではない。

さらに各textは先頭の文字列partのみを最大2,000文字にし、channel・end_turn・相関metadata等を返さない。これは一覧・表示用途の射影として理解できるが、完全回答の証拠として使ってはいけない。

今回の合成試験では5,000文字の回答が2,000文字で返り、切り詰めを示すフィールドがないことを確認した。選択branchのある正常ケースは兄弟branchを除外する一方、current_node欠落時には両branchが出力された。全履歴処理が常に混ざるという主張ではなく、そのfallback条件を再現した結果である。[G]

## 5. suphotP/chatgpt-api の経路と境界

対象HEAD：`f998a6d83f324cb3187396dd7efced0c40f29601`。

外側のOpenAI互換APIがprovider-neutralな `ChatRequest` へ変換し、`ChatGPTWebTransport.stream_chat()` が本文構築、生成前処理、HTTP stream受信を行う。stream処理は同期HTTPをdaemon threadで動かし、asyncio.Queueを通して非同期側へ渡す構成である。

`ChatRequest` にはnew/follow-upに関係するconversation_id・parent_message_id、action、model、effort、metadataがある。ただし `captured_request_json` を明示した場合、本文構築器はその辞書を優先して返す。この上書き機能を「新しいrequest指定と必ず整合するテンプレート」と誤認すると、古いmodelや本文が残る。今回、その明示override条件を試験した。通常のoverrideなし呼び出しまで新入力を無視するという証拠ではない。

`token refresh` という関数名も、ログイン認証の更新と、1回の生成に必要な準備の更新を区別して読む必要がある。長期セッションの再認証を実装したことと同一視しない。

### API tool_calls はnative connectorではない

外側の互換APIには、Webモデルへツール要求をJSON形式で返すよう指示し、その出力をAPIのtool_callsへ変換する経路がある。クライアントがそれを実行して結果を次の入力へ戻す。native MCP connectorで同じChatGPT応答中にツールが往復する構造とは違う。

この変換を追加する場合、tool名・引数schema・call ID・出力との対応・JSON以外の本文・拒否・未対応形式を扱う必要がある。モデル出力は呼び出し要求であり、実行の許可そのものではない。外側クライアントの既存の承認を残す。これは子のGUI利用を禁止する要件ではない。

### 再現した受信上の境界

実際の `_iter_post_conversation` のASTだけを抽出し、HTTPをメモリー上のfakeへ置き換えて試験した。

| 合成入力・状態 | 実際に観測した挙動 | Anywhereへ取り込む際の対応 |
|---|---|---|
| `data: ` の空白あり一行JSON | 読める | 正常系の対照 |
| `data:` の直後にJSON、空白なし | 読み落とす | SSEの任意の1空白を正しく扱う |
| 1イベントのJSONを複数data行に分割 | 読み落とす | 空行までdataを集め、改行を挟んで結合する |
| 空行で終わらない最後の一行JSON | イベントとして返す | 一般SSEではEOFを空行の代わりにしない |
| replaceで `Hel`、次に `Hello` | 両方textとして返す | 単純連結では `HelHello`。差分演算とsnapshotを区別する |
| 未知のoperation、textパスと値だけあり | textとして返す | 未知operationは本文へ昇格させない |
| 異なるconversation IDが複数ある | 最初を選ぶ | candidateの整合性確認と拒否が必要 |

これらは特定関数の特性であり、現在のChatGPTが必ずこの形式を返すという主張でも、外側の全層が必ず同じ誤表示をするという主張でもない。実サービス通信は行っていない。

### キャンセル後に同時実行枠が失われる

`AccountRouter.acquire` はthreading.Semaphoreの取得を `asyncio.to_thread` で待つ。coroutineをキャンセルしても、既に動いているthreadのacquireは消えない。後で枠が空くとthreadだけが枠を取得し、呼び出し元へAccountLeaseが返らない。

試験では枠を先に塞ぎ、待機開始をイベントで確認し、coroutineをcancelした後に枠を解放した。background threadは取得に成功したが、新しい呼び出しは枠を取得できなかった。最後は合成枠を明示解放し、試験threadを残していない。

対処は単に例外を無視することではない。同一event loopならasyncio.Semaphoreを使い、取得成功直後の所有権移転を明確にする。跨ぐthreadが不可避なら、遅延取得後の取消検出・一度だけの返却・shutdown joinを管理する。新規参照試験ではasyncio.Semaphore版がcancel後も容量を保つことを確認した。

このほか、受信thread→queueが無上限であり、イベントの全件保持もあることをコードで確認した。負荷・停止時の影響は今回実測していないため、再現不具合の件数には加えない。[S]

## 6. CLIProxyAPIとchat2apiの分岐を追った結果

前回はCLIProxyAPIの宣言だけを読んだが、今回は分割された `codex_executor_execute.go` まで確認した。既定baseは `https://chatgpt.com/backend-api/codex` で、末尾へ `/responses` を加える。独自baseURL等の別設定はあり得るが、この既定Codex経路は通常Chatの新規会話生成ではない。

外側の入力をCodex形式へ変換し、モデル・reasoning設定・tool schemaを整え、上流のoutput item・terminal eventを互換形式へ戻す。公開されるresponse ID、cache/session ID、Web会話IDは同じ概念ではない。変換器が出したresponse.completedを、通常Chatでのinput/final一致の代わりにしない。

`aurorax-neo/chat2api` では同じResponses入口でも、画像生成toolのある要求はCodex画像経路へ、通常textはChat Completions相当へ変換して通常Chat経路へ進む。画像経路には失敗後の別経路fallbackもある。リポジトリ全体を単一の送信方式と分類したり、1機能の成功を全機能へ一般化したりできない。

Anywhereの目的に必要なのは通常Chatの直接送受信である。Codexモデル選択UIやResponses完全互換層を先に作っても、通常Chat側の生成前処理は解決しない。[C][A]

## 7. 認証、生成準備、HTTP、モデル実行を分離する

| 層 | 責任 | 完了と誤認してはいけないもの |
|---|---|---|
| 正規のセッション | 利用者とaccount、期限、更新・失効 | 他製品のログインが存在するだけ |
| 生成前の準備 | その要求で必要な初期化・生成準備を成立させる | 過去のrequestが持っていたfield名だけ |
| 本文・リソース | 選択model/effort、本文、new/follow-up、添付・connector | UIラベルをslugとみなす、未対応指定を削る |
| HTTP transport | 一回のdispatch、応答、途中の切断、resource上限 | 200だけ、socket書き込み成功だけ |
| stream projector | root、差分、本文の種類、候補IDを整合的に解釈 | textらしい文字列の単純連結 |
| receipt/final確定 | 保存input、account、相関、一意なfinalと終了証拠 | wrapper自身のcompleted、EOF、[DONE] |
| 実行寿命 | 観測者とworkerの分離、取消、更新、再起動 | 親のtool呼び出しが終わったこと |

生成準備のうち、公開実装が外部の検証処理やブラウザー同等性へ依存する部分は、完全な通常API仕様として確立できていない。今回、それらのtoken生成・偽装・保護回避処理は実装・実行していない。既存のKeychain拒否も再試行していない。

通常の認証済みセッションがあっても、読み取り許可と生成要求の受理は別である。各種prepared値やheaderを固定値で足せば必ず動くという結論は得られていない。providerの現在の正規経路において、各値の発行元、有効範囲、失効、accountと要求との束縛を確認する必要がある。

この未確定部分を `PreparationProvider` の実装境界として残す。返り値の提案は、protocol版、対象account、内容digest、許可された送信先、request-scopedな秘密のハンドル、有効期限である。これは新しいサービス契約が発見されたという意味ではなく、未知部分を他層へ漏らさないための設計である。未対応ならdispatch前に明示拒否する。

## 8. Anywhereに追加する実装の分割

以下の型名・ファイル名は提案であり、本コミットで製品機能として追加したものではない。

### 8.1 `HTTPDispatchPlan`：ローカルの意図

operation ID、provider account、明示model/effort、本文・リソースのdigest、new/follow-up、追送先conversationと期待predecessor、契約版を保持する。読み取り用のUIラベル、相関情報、providerへ実際に送る項目を混同しない。元の本文は秘密情報を含み得るので診断ログへ複写しない。

新規のrootやparentを固定値で決め打ちしない。上流実装には異なる時期・異なる固定値があり、現行Anywhereの観測では初回でIDが省略されたケースもある。provider契約の版ごとに明示し、未知の版は未対応とする。

### 8.2 送信予約とinput/accountの原子的な保存

既存 `SubchatSubmissions` の所有者、一意性、同一IDの内容一致、CASを維持し、`begin_http_send` 相当で送信予定inputとaccount・planを一度に保存する。保存失敗ではPOSTしない。既存の別transactionを外側のwith文で囲むだけで原子的になったとはしない。

会話初期化がremote stateを作るなら、それを「副作用なしのprepare」へ入れない。初期化、生成準備、生成POSTの段階と証拠を分ける。初期化POSTの冪等性も推測しない。

### 8.3 独立transportと受信器

browser factoryを持たないHTTP backendを追加する。通常Chatの正規に利用できる生成前処理は別providerとして渡し、transportは秘密をMCP引数・argv・永続台帳・エラー本文へ出さない。接続先は選択したproviderに固定し、任意URLやcredentialの転送を許さない。

redirect、生成POSTの自動再試行、モデル・account・ブラウザーへの黙ったfallbackを止める。ただし、設定で再試行0としたことはprovider側exactly-onceの保証ではない。送信結果不明を同じUUIDで再送しても安全とは限らない。

受信を「バイトのSSE framing」「JSON event schema」「message stateの差分適用」「公開progress」「確定final」に分ける。replace、append、remove、root snapshotを区別し、未対応operationはプロトコル変更として止める。複数assistant message・channelを跨いで連結しない。internal reasoningを最終textとして公開しない。

今回の `sse_reference.py` は最初のframing層だけの参照実装である。UTF-8/BOM、CR/LF/CRLF、任意の1空白、複数data行、未完了EOF、line/event/total/count上限を扱う。不正UTF-8の拒否は明示的なlocal policyで、ブラウザーEventSource全体と同等という意味ではない。実アカウントへの送信コード、認証コード、再接続コードは含まない。

### 8.4 既存historyでreceiptとfinalを確定

ストリームで得た会話IDは候補として早期保存する。別input、別account、矛盾するIDは拒否する。生成要求の200、EOF、[DONE]は受領・最終回答の代わりにしない。

現行のflat history、外部実装が扱うgraph historyを別adapterにし、最初は現在検証済みの方式を維持する。汎用再帰検索で最初/最後のIDを採用しない。graphで選択branchが分からなければ、全ノードの時刻順一覧を完全回答にしない。

本文、明示resource、保存input、一意のcorrelation、finalのstatus/end_turnと非空textを検証する。現在のoptionalなis_complete/finish_detailsを再び常時必須に戻さない。async finalは既存の明示的なasync根拠と安定turn ID条件を保つ。interruptedは保存して、partialをcompletedへ昇格させない。

### 8.5 worker・lease・容量

親の短いwaitと生成streamを分離し、既存engineの管理下でworkerがstreamを所有する。内側watchのためだけに外側Direct MCPのidleを無限にする方法は採らない。完了タスクが実行枠を保持する既知問題も、activeと未受領結果を分離して修正する。

取消可能な待機をthreading.Semaphore＋to_threadに任せない。使い分けが必要なら取得・所有権移転・取消・返却を一つの管理単位にする。local asyncio.Semaphoreは同一process用であり、複数processやprovider側のrate limitまで解決しない。

同じ稼働中DBへ別workerが毎回 `Ledger(...)` を作らない。既存初期化処理が一般operationsを再起動扱いにする。engineのconnectionまたは限定した更新RPCを用い、DB接続とrestart recoveryを分離する。再起動したworkerは保存意図・権限・期限・認証を検証し、sendingを再送しない。

shutdownは新規受付停止、既存streamの上限付きdrain、状態保存、資源終了を分ける。idle期限、取消、クライアント切断はproviderで生成が停止した証拠ではない。

### 8.6 Codex向け外側APIは最後

将来必要ならResponses互換層を薄く追加する。response/item/output index、delta、completed/incomplete、tool call ID、function output、context compactionを明示対応させる。Webモデルに全文脈をuser入力として渡すことを、native system/developer roleの同じ権限だとは扱わない。

今回のsubchatの目的ならMCPから既存operationを操作できれば足りる。外側APIの完成のために、通常Chatの送信が完成したと見せたり、Codex専用upstreamへ置き換えたりしない。

## 9. 最小の実装PRと合格条件

| 順序 | 実装単位 | 合格条件 |
|---|---|---|
| 1 | 独立SSE framingと版付きevent projector | 境界分割、UTF-8、改行、巨大イベント、未知op、別message/別channel、replaceを検査 |
| 2 | immutable planと原子的送信予約 | owner/account不一致、同じID別本文、commit失敗でゼロPOST。未知送信の再送なし |
| 3 | 正規のsession/preparation adapter | 準備の出所と契約を確認。未対応は副作用前に明示。認証欠如を別モデルやブラウザーで代用しない |
| 4 | 通常Chatの一回の新規生成 | 認証後はブラウザーを作らず、一回のPOSTと保存input・最終回答を照合 |
| 5 | 同一会話の追送と再起動回収 | expected predecessor、別branch、人間の同時送信、ID保存前後のcrash、不明結果を試験 |
| 6 | engine-owned worker | 親wait終了、外側MCP idle、取消、lease失効、late error、終了待ち、容量を検査 |
| 7 | 添付・native connector | 送信指定、保存されたresource、実際のtool効果を独立に確認 |
| 8 | 配布・導入済み接続での非干渉 | source/wheel/runtimeを照合し、new/follow-up/回収中の画面を継続観測 |

段階1・2・6を合成HTTPで実装しても、段階4のprovider受入を達成したことにはしない。初回の対話認証を許すかどうかと、認証後に毎回ブラウザーを必要としないことも別に記録する。

## 10. 今回の試験結果の読み方

上流特性試験は12件、失敗0・エラー0。これは問題のないライブラリと認定した数ではない。記録した良くない挙動も含め、指定された関数が実際にどう振る舞うかをassertした試験である。ASTで選択した定義だけを実行し、上流モジュールの初期化、credential loader、challenge処理、ブラウザーを実行していない。

新規framing/transport参照試験は21件、失敗0・エラー0。UTF-8/BOMを含む入力の全71分割位置を検査した。localhostへのPOSTは2回で、一つは先頭イベント→短いwaitのtimeout→stream継続→末尾取得、もう一つは307でredirectしないケース。外部接続と子process起動の試行は0回。これは実HTTPの受信挙動であり、モデルの生成やprovider認証の検証ではない。

合成のsame-thread/asyncioキャンセル改善例も含むが、上流アプリ全体を修正・再試験したわけではない。前回の12/16 live等の作者件数を今回のテスト数に加算していない。

## 11. 未確認と保留

通常Chatの独立HTTP新規生成は今回も未実行であり、成功したとは記録しない。正規の認証済みsessionと独立した生成準備が確立していない。過去の認証拒否を迂回したり、未公開外部処理を調達して動かしたりしていない。

JSON差分の全バリエーション、全model、全account、stream handoffのWebSocket版、画像・音声、全ツール形式、外側Codexの全protocol版は未網羅である。対応範囲を段階別に増やすべきで、任意payloadの素通しを「全部対応」としない。

公開前に同梱runnerでの再実行と既存package/runtime試験を追加で要求したが、その複合コマンドはツール境界で実行前に拒否された。同じ試験を別経路で再試行していない。上記12件・21件は先に直接実行した結果であり、この追加確認の成功ではない。runnerのend-to-end実行と今回のpackage試験は未確認として残す。公開する参照コード・試験コードは、実行済み原本とハッシュを照合する。

元のproduction source、常駐engine、ブラウザー、権限を変更しない。今回GitHubへ追加するのは研究文書、参照コード、試験コード、秘密を除いた結果・ソースmanifestであり、productでHTTP生成を有効化するコミットではない。

## 12. 一次資料と固定版

[M] [miuuyy/codex-chatgpt-web architecture](https://github.com/miuuyy/codex-chatgpt-web/blob/eaf4f09ae92d4dc4429fa597b0861663138f08f8/docs/architecture.md)、同commitの `src/adapters/chatgpt-web/mcp-server.ts`、`turn-broker.ts`、`retry-policy.ts`。

[G] [gpt2agent server](https://github.com/robotlearning123/gpt2agent/blob/38a78fe8affb7d23955f01b45a98924b3e7714ab/gpt2agent/server.py)、`gpt2agent/sse.py`、[history projection](https://github.com/robotlearning123/gpt2agent/blob/38a78fe8affb7d23955f01b45a98924b3e7714ab/gpt2agent/tools/conversations.py)、README、`artifacts/verify/sentinel-restore-20260917/receipts.md`。作者のlive記録日と本調査日は別。

[S] [suphotP transport](https://github.com/suphotP/chatgpt-api/blob/f998a6d83f324cb3187396dd7efced0c40f29601/chatgpt_api/providers/chatgpt/transport.py)、[compat server](https://github.com/suphotP/chatgpt-api/blob/f998a6d83f324cb3187396dd7efced0c40f29601/chatgpt_api/api/openai_compat.py)、`chatgpt_api/core/types.py`、README。実際に実行した定義と元の行範囲は試験結果JSONに記録。

[C] [CLIProxyAPI Codex executor](https://github.com/router-for-me/CLIProxyAPI/blob/555662940411a07460e9d24d14477a5f50dffdb5/internal/runtime/executor/codex_executor_execute.go) と同commitの `codex_executor_request.go`。

[A] [aurorax Responses routing](https://github.com/aurorax-neo/chat2api/blob/dbf6bf39686348449c25de3b6131ca733f448deb/app/service/responses.go)、同commitの `responses_chat.go`、`responses_codex.go`。

[W] [WHATWG SSE](https://html.spec.whatwg.org/multipage/server-sent-events.html)：UTF-8、改行、field/value、空行dispatch。EventSourceの再接続規則を、通常Chat生成POSTの再送許可へ転用しない。

[H] [HTTPX async streaming](https://www.python-httpx.org/async/) と [Playwright APIRequestContext](https://playwright.dev/python/docs/api/class-apirequestcontext)：HTTPクライアント機能の仕様とChatGPTの受理を区別する。

[O] [OpenAI Codex custom providers](https://developers.openai.com/codex/config-advanced/)：クライアント側接続設定であって、通常Chatの上流認証・生成準備を保証する資料ではない。
