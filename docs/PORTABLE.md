# ランタイム同梱配布物

開発機の信頼済み python-build-standalone と、プラグインのチェックサム付き wheel・
固定依存一覧から ZIP を作る。利用者の PC に Python/uv の手動導入は不要。
本体コードは共通だが、Python とネイティブ依存のため OS/CPU ごとの配布物が必要になる。

ビルド機で、対応する Python から実行する。uv の依存キャッシュを事前に準備する。

```sh
uv run --offline python scripts/build_portable.py --output dist/anywhere-portable.zip
```

`--runtime` で信頼済みベースランタイムを明示できる。仮想環境と外部へ出る symlink は
拒否する。BUILD マーカーは由来の証明ではないため、取得元の信頼性はビルド側で確認する。
既存の出力ファイルは上書きしない。配布 ZIP は dist に置き、リポジトリへ追加しない。
Python と第三者依存のライセンスは runtime 内に保持し、本体の MIT と区別する。

展開した Anywhere Computer フォルダー内で実行する。

```sh
./anywhere chatgpt-setup --state-dir "$HOME/Library/Application Support/Anywhere Computer/chatgpt"
```

上は macOS の例。Windows は `anywhere.cmd`、Linux は `./anywhere` を使い、
利用者の状態保存ディレクトリを明示する。公開 HTTPS、所有者認証、ChatGPT 側の
接続登録は別途必要。GUI インストーラー・署名・公証・公開配布はまだ含まない。
macOS は実行権限を保つ標準のアーカイブユーティリティまたは ditto で展開する。

2026-09-09: macOS arm64 / CPython 3.12.13 で実物を生成し、日本語と空白を含む
別ディレクトリへ ZIP から展開。PATH を /usr/bin:/bin に限定し、PYTHONHOME と
PYTHONPATH を無効な場所に向けても CLI 起動・依存 import・日本語ファイルの書込/読込・
別 Python プロセスを使う正規表現検索が成功した。Linux ARM64 同梱版も Ubuntu の隔離
ゲストで生成・検証済み（詳細は `research/2026-09-09-linux-guest-verification.md`）。
Windows の同梱配布物は実機未検証。

配布物自体のエージェント試験は次で再実行できる。信頼済み ZIP を展開したディレクトリを
指定する。実行時に OS 資格情報ストアへのアクセスが必要で、試験専用の一時資格情報を
作成し、終了時に削除する。既存のユーザーエージェントの状態には接続しない。

```sh
uv run --offline python scripts/verify_portable.py '/path/to/Anywhere Computer'
```

2026-09-09: 同梱 Python で別プロセスの CLI `serve` を起動し、ファイル操作・正規表現
ワーカー・端末セッションの接続終了後の入力/出力・処理中の停止拒否・処理後の正常終了を
確認した。試験用 Keychain 資格情報の削除も再照会して確認した。この試験はローカル
接続であり、インターネット越しの切断回復や ChatGPT 側の実利用を証明するものではない。

同梱版の公開 HTTPS 試験には、既存の `scripts/verify_internet.py` を同梱 Python の
`-I` で実行し、`--expected-runtime-root` に同梱 runtime ディレクトリを指定する。
開始前と処理終了時にインタープリターおよび読み込まれた本体モジュールの配置を検査し、
開発ツリーなどからの混入を拒否する。トンネル監督の子 Python も `-I` で起動する。
公開試験には別途 cloudflared と試験専用トンネルが必要で、これらは ZIP に含まれない。

公開試験の結果は `docs/research/2026-09-09-portable-internet-verification.json` に記録する。
同一 Mac から公開エッジを経由した試験であり、別端末や ChatGPT UI からの接続ではない。
通常の常用プロフィールを試験へ流用せず、専用マーカー付きの試験プロフィールだけを使う。

同梱 ZIP には、設定開始用の `Setup ChatGPT.command`（macOS/Linux）または
`Setup ChatGPT.cmd`（Windows）も含める。macOS/Windows は関連付けされた端末から
開く入口として用意し、Linux は端末から `.command` を実行する。どちらも同じ
`setup_chatgpt.py` を同梱 Python の `-I` で起動する。状態ディレクトリは本体と同じ
OS 別規則で決めた場所の `chatgpt` サブディレクトリで、配布物を移動しても分離する。
既存の ANYWHERE_STATE_DIR 指定は本体の規則に従う。明示引数で変更もできる。
終了時は対話端末なら Return を待ち、設定結果がすぐ消えないようにする。
これは対話設定の入口であり、公開ホスティングや ChatGPT の認証を自動完了するものではない。

macOS で空白・日本語パスへ再展開し、この入口から `--help` を実行できることと、
新しい配布物のエージェント起動・ファイル操作・端末再接続・後片付けを検証した。
Finder のダブルクリック動作・Gatekeeper/公証・Windows の実起動は未確認。

Quality CI では三 OS のそれぞれで同梱 ZIP を生成し、日本語・空白パスへ展開した後に
`verify_portable.py --runtime-only` を実行する。このモードはファイル操作と分離した
子 Python の実行を検査し、`native_agent_tested: false` を明示する。CI の資格情報ストア
可用性に依存せず配布構造を検査するためのモードであり、通常モードの実エージェント・
Keychain/Windows Vault/Secret Service 検証を代替しない。成功した ZIP は private repo の
Actions artifact として 14 日保持する。ジョブ定義の追加だけでは Windows/Linux の成功
実績としない。実際のジョブ結果は別途確認する。

ZIP の公開は出力ディレクトリと同じファイルシステム内の staging で全体を完成させてから、
排他的な hard link 作成で行う。既存ファイルを置き換えず、作成失敗時に最終ファイル名で
途中の ZIP を残さない。hard link 非対応のファイルシステムでは失敗を返すため、
その場合は対応するローカルファイルシステム上でビルドする。

検証は新しく展開した配布物へ、初回起動前に行う。manifest に載ったファイルのハッシュ
だけでなく、実ファイル集合との完全一致を検査し、一覧外の Python/.pth/バイトコード等も
拒否する。通常起動が生成した __pycache__ も一覧外なら拒否するため、再試験は新しい
ディレクトリに ZIP を展開してから行う。検査は余分なファイルを自動削除しない。
manifest 自体は署名されておらず、信頼できる配布元から取得した ZIP の内部整合性検査で
ある。配布元の真正性や、manifest とコードを一緒に差し替える攻撃への保証ではない。
ビルダーは循環コピーを避けるため、ランタイム内のディレクトリ symlink を拒否する。

新しいビルド機では、`uv sync --locked` が成功していても、オフライン再解決に必要な
インデックス情報がキャッシュされているとは限らない。`--allow-downloads` を指定すると、
ビルド時に依存の取得を許可する。`--require-hashes` と固定依存一覧は維持する。
指定しない場合は従来どおりオフラインで、必要情報がなければ失敗する。配布物の実行には
uv もこのダウンロード設定も不要。三 OS の CI では新しい環境のためこのオプションを使う。

Linux ARM64 でも通常モードの実エージェント試験が成功した。Ubuntu 24.04.4 の隔離
ゲストで GNOME Secret Service を使い、認証付き起動・端末再接続・停止・試験用資格情報の
削除を確認した。詳細は `research/2026-09-09-linux-native-vault-verification.md`。
利用可能で解錠済みの OS 資格情報サービスが必要で、Windows や ChatGPT UI の実測ではない。

対話セットアップ完了時には、同じ状態保存先を指定した再開・起動・診断のコマンドを
そのままコピーできる形で表示する。`commands` の JSON は AI 向けで、その後の通常テキストが
人間向けのコピー用。macOS/Linux は POSIX シェル、Windows は PowerShell と明示する。
実行中の Python の絶対パスと `-I` を使用するため、PATH に別の Python があっても選ばない。
配布物を移動した場合は新しい場所からセットアップを再開してコマンドを再生成する。
開始コマンドはフォアグラウンドの `remote-watch` であり、その端末を開いたまま使う。
自動起動登録、公開ルートの作成、資格情報の入力をこの表示だけで実行することはない。
