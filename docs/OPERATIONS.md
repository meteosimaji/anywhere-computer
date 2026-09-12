# 起動・常駐・接続の詳細

初回導入は[README](../README.md)を参照してください。ここでは既存の設定を使う
起動・停止・診断と、複数端末の管理を説明します。

## 起動

Python 3.12 と [uv](https://docs.astral.sh/uv/) を利用します。

```sh
uv sync --locked --python 3.12
uv run anywhere start
uv run anywhere status
```

MCP クライアントには、このリポジトリを作業ディレクトリにして
`uv run --locked anywhere mcp` を設定します。エージェントが停止していれば起動します。
資格情報は OS の保管機能を利用します。Linux では Secret Service/KWallet などの
利用可能な保管機能が必要です。

```sh
uv run anywhere doctor
uv run anywhere stop
```

実行中の端末セッションがある場合、`stop` は停止せず理由を返します。
`start` はログイン時の自動起動を設定しません。OS管理の常駐には
`autostart-install` を使います。[常駐モード・停止・移行の仕様](PERSISTENCE.md) を参照してください。

`uv run anywhere autostart-preview --state-dir <設定ディレクトリ>` で、同じ
`remote-watch` を macOS の LaunchAgent、Linux のユーザー unit、Windows の
ログイン時タスクから起動する定義を JSON 内で確認できます。ファイル作成やサービス登録は
行いません。`registration_state: unverified` は登録状態を調べていないことを示します。
定義の生成だけでは、資格情報ストアへのアクセスや接続の準備完了を確認できません。
登録・解除には `autostart-install`、`autostart-uninstall`、状態確認には
`autostart-status` を追加しています。いずれも同じ `--state-dir` を指定します。
登録には HTTP 設定、所有者の資格情報、トンネル資格情報、利用可能な接続子が必要です。
登録記録と OS の定義が一致しない場合は変更を拒否し、解除を確認できない場合は記録を
保持します。解除しても資格情報は削除しません。`native_running` は OS 側の実行状態であり、
公開 URL への接続成功を意味しません。三 OS の隔離 CI で試験プロセスの実登録・起動・
停止・解除と最終照会での不在を確認済みです。
新規登録は常駐ポリシー2を使用します。`autostart-start` は停止済みの一致する登録を
明示的に開始し、`autostart-upgrade` は実行中のPythonへ登録を移行します。
macOSの現在のログインセッションでは実接続と監督プロセス異常終了後の復旧を確認しました。
実際の再ログイン・OS再起動・スリープ復帰は未検証です。
macOS の状態照会は `launchctl print` の出力解析を含みます。この形式は安定した API として
保証されていないため、OS 更新後は実機試験が必要です。認識できない定義は変更を拒否します。
Windows のログイン時タスクはログオン済みのユーザーセッションで動作します。
登録できても即時起動できない環境があり、登録状態と `native_running` を別々に確認します。
Windows の起動定義では、スケジューラーによる環境変数展開を防ぐため `%` を含む
実行パス・設定パスを拒否し、長さを 260 UTF-16 単位以内に制限します。

ログイン時とシェルの `PATH` が異なる環境では、`remote-watch`、`remote-serve`、
`tunnel-run` に `--connector <接続子の絶対パス>` を指定できます。監督プロセスの
再起動後も同じ指定を引き継ぎ、指定先のバージョン検証・実行に失敗した場合は
別の接続子へ切り替えません。`autostart-preview` にも同じオプションを指定でき、
起動定義に保存する引数を確認できます。プレビューはバイナリを実行しません。
`remote-doctor --connector <同じ絶対パス>` では、そのパスの実行ファイルを確認できます。
診断は接続子を実行せず、バージョンとプロセスの稼働状態は `unverified` として返します。

`remote-watch` の起動時に、選択済みの資格情報ストアの読み取りが失敗した場合は、
1・2・4・8・16 秒の待機を挟んで最大 6 回確認します。未登録・破損は再試行せず、
別の保管先への切り替えや資格情報の再作成もしません。待機は Ctrl+C で中断できます。
待機中は同じ設定の別サーバー起動とトンネル資格情報の変更を拒否します。
これらを変更する場合は、先に待機を中断してください。
これは読み取りの試行回数と待機時間の制限です。OS 側の読み取り呼び出し自体の停止や、
資格情報バックエンドの選択に失敗した場合の復旧は、この処理の対象に含みません。

`http-watch` と `remote-watch` は、子プロセスの起動・再起動待ち・上限到達・正常終了・
中断・起動失敗の最後の観測を状態ディレクトリへ保存します。`http-doctor` と
`remote-doctor` の `supervisor_history` から、観測時刻、再起動回数、最後の終了コードを
確認できます。保存するのは最新の一件で、コマンド引数、資格情報、標準エラー本文は含みません。
これは過去の観測です。強制終了などで更新されない場合があるため、現在の稼働状態は
`current_process_state: unverified` とし、到達性の診断と分けています。
`remote-watch` は、選択済み資格情報ストアの確認中・利用不可・資格情報の拒否・確認成功も
同じ記録に保存します。`startup_attempt` は資格情報の確認回数で、子プロセスの
`restart_attempts` と区別します。資格情報自体や例外本文は保存しません。
設定の読込、保管先の選択、接続子の初期バージョン検査の失敗も、それぞれ
`configuration_error`、`credential_backend_error`、`connector_check_error` として記録します。
別の HTTP サーバーや接続子が必要なロックを保持している場合は `startup_conflict` を残し、
既存のプロセスを引き継いだり停止したりせず終了します。
記録を残すため、`remote-watch` は指定した状態ディレクトリを作成してから起動を試みます。
接続子内部の詳細原因や、プロセスが生きたまま公開経路が切れた状態は、この記録だけでは
判定できません。
`remote-doctor` の `connector.history` には、接続子を監督する処理の最後の観測も表示します。
上限停止時の回数・終了コードに加え、`last_failure_kind` の `child_exit` は非ゼロの
プロセス終了、`connector_error` は起動や資格情報の受け渡しなどでのエラーを示します。
資格情報ストアの利用不可と、秘密の受け渡し用資源を解放できなかった場合も区別します。
接続子の出力本文は記録しないため、DNS や提供元の認証などの詳細には別の確認が必要です。

`doctor` は起動・停止・設定変更をせず、停止中、古い接続情報、資格情報ストアの
利用不可、プロセスは存在するが応答しない状態、別ビルドの稼働を区別します。
JSON の `state` と `action` に結果と対処を返し、`ready` 以外は終了コード 1 です。
`unresponsive` だけでは停止や認証失敗の原因は断定せず、作業を保持します。

既存の SSH 接続先に同じ版を導入している場合は、次の MCP 起動経路も使えます。
接続先の `anywhere` コマンド、認証設定、信頼済みホスト鍵が必要です。

```sh
anywhere remote-mcp --ssh-host windows-lab
```

事前に認可済みの HTTP 接続プロフィールには、次の起動経路もあります。
同じ接続先に所有者認証とクライアント登録を設定済みの場合、まずブラウザーで
許可する機能を確認します。認証情報のコピーは不要で、OS の資格情報ストアへ保存します。
サーバーの設定・起動は [HTTP サーバーの手順](HTTP-SERVER.md) を参照してください。
常設 HTTPS の接続口と証明書管理の自動構築は引き続き開発中です。

```sh
anywhere login --resource https://your-agent.example/mcp --client-id registered-client --profile laptop --scope files_read --scope files_write --scope operations_get
anywhere http-mcp --resource https://your-agent.example/mcp --client-id registered-client --profile laptop
```

認可の戻り先は、その都度空いているローカルポートを使います。サーバーには
`http://127.0.0.1/oauth/callback` と `http://[::1]/oauth/callback` を登録します。
コマンドは外部ブラウザーを開き、最長5分間承認を待ちます。待受は接続開始時だけ
ローカル IP に作られ、完了・拒否・失敗・時間切れで閉じます。

HTTP 応答を失った操作は再送せず、既知の操作 ID と `unknown` 状態を返します。
接続を作り直して `operations_get` で結果を照会できます。詳しい前提と制限は
[HTTP クライアント](REMOTE-TRANSPORT.md#http-client-and-stdio-connector)を参照してください。

## 複数端末

ローカル接続では `devices_list` → `devices_tools` → `devices_call` の順に、登録した
端末を明示的なIDで指定できます。通常のツールはローカルを対象としたままです。
認証・再送の契約は [会話からの端末指定](DEVICE-ROUTING.md) を参照してください。

既存の SSH 設定のホスト名、または HTTP の接続プロフィールを名前で登録します。
秘密鍵・パスワード・トークンは登録情報に含めません。

```sh
anywhere device-add --name "Windows Lab" --ssh-host windows-lab
anywhere device-add --name "Linux Lab" --ssh-host linux-lab
anywhere device-add-http --name "Home" --resource https://computer.example/mcp --client-id desktop-client --profile home
anywhere login --device-name Home --scope files_read
anywhere http-mcp --device-name Home
anywhere devices
anywhere device-status --device-name Home
anywhere remote-mcp --device <登録時に返されたID>
anywhere device-rename --device <ID> --name "Windows Arm64"
anywhere device-remove --device <ID>
```

表示名を変更しても ID と認証プロフィールは維持されます。`--device` は ID、
`--device-name` は表示名を指定し、両方の同時指定はできません。
HTTP 接続先を名前や ID で選ぶ場合、resource/client/profile の上書き指定は拒否します。
同じ resource/client/profile の重複登録も拒否します。登録は接続プロフィール単位であり、
登録件数が物理端末の台数を証明するものではありません。

状態確認は明示的に実行します。HTTP は MCP の接続確立とツール一覧だけを確認し、
端末操作ツールは実行しません。確認中に認証トークンを更新する場合は OS 資格情報ストアを更新します。
`ready` は確認した時点での応答を意味し、60 秒後は現在の状態を `unknown` とし、
最終確認結果と時刻を残します。時計が巻き戻った場合も `unknown` です。
同時確認の表示は最後に完了した結果を採用します。
SSH の失敗した確認は `unreachable` です。端末の電源断、
SSH 認証失敗、リモートコマンド失敗のどれかは、この結果だけでは断定しません。
HTTP は再認可が必要なら `authorization_required`、資格情報ストアを使えなければ
`credential_unavailable`、通信や応答を検証できなければ `not_ready` です。
`authorization_required` には更新結果不明も含まれ、権限失効を断定する表示ではありません。
`device-remove` はローカル登録の削除であり、認証権限の失効、資格情報の削除、
リモートエージェントの停止は行いません。同じ resource/client/profile を同じ状態ディレクトリへ
再登録すると、保存済み資格情報を再利用します。既存の SSH 登録 DB は初回起動時に
トランザクション内で移行し、ID・名前・接続先・観測記録を保持します。
