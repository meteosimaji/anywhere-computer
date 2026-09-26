# ランタイム同梱配布物

## 配布物の確認

公開済みのOS別実行用ZIPと版数は[GitHub Releases](https://github.com/meteosimaji/anywhere-computer/releases)で確認します。
GitHubのソースZIPには実行用Pythonは含まれません。以下は同梱ZIPの生成・利用手順です。
CIのruntime-only試験は各OSでの資格情報ストアやChatGPT接続の実機受け入れを代替しません。
macOSの常駐更新手順は[更新ガイド](UPDATING.md)を参照してください。
日付付きの実機検証記録はローカルの履歴資料に保管しています。

## ビルドと利用

開発機の信頼済み python-build-standalone と、プラグインのチェックサム付き wheel・
固定依存一覧から ZIP を作る。利用者の PC に Python/uv の手動導入は不要。
本体コードは共通だが、Python とネイティブ依存のため OS/CPU ごとの配布物が必要になる。

ビルド機で、対応する Python から実行する。uv の依存キャッシュを事前に準備する。

```sh
uv run --offline python scripts/build_portable.py --output dist/anywhere-portable.zip
```

macOS でネイティブ GUI 操作を含める場合は、同じ Mac で補助プログラムをビルドし、
`--gui-helper` に絶対パスを渡す。補助プログラムがない ZIP では
`gui_native` は利用不可と報告される。Quality CI の macOS 配布 ZIP にはこれを含める。

```sh
mkdir -p dist
swiftc native/macos/AXHelper.swift -o dist/anywhere-gui
uv run --offline python scripts/build_portable.py \
  --gui-helper "$(pwd)/dist/anywhere-gui" --output dist/anywhere-portable-macos.zip
```

`--runtime` で信頼済みベースランタイムを明示できる。仮想環境と外部へ出る symlink は
拒否する。BUILD マーカーは由来の証明ではないため、取得元の信頼性はビルド側で確認する。

macOS の文書プレビューを手動インストール不要にするビルド経路は `--renderer-bundle`
です。ビルド担当者が検証済みの実体ディレクトリを絶対パスで渡します。ディレクトリには
`LibreOffice.app/Contents/MacOS/soffice`、`bin/pdfinfo`、`bin/pdftoppm` の
実行可能ファイル、`LICENSES/LibreOffice.txt`、`LICENSES/Poppler.txt`、
`SOURCES.json` が必要です。JSON には `libreoffice` と `poppler` それぞれの
`version`、`binary_source`、`source_code` を記録します。例えば:

```sh
uv run --offline python scripts/build_portable.py \
  --renderer-bundle /absolute/reviewed-renderer \
  --output dist/anywhere-portable-renderer-macos.zip
```

ビルダーはこの一式を `renderers/macos` に同梱し、manifest にハッシュを記録します。
実行時は同梱版を優先し、一部が欠けた場合はホスト上の実行ファイルと混在させません。
通常の ZIP ビルドと Quality CI の公開候補には、現時点ではこのオプションを指定して
いません。そのため、その配布物は従来どおりローカルの LibreOffice と PDF ツールを
必要とします。正式な配布候補にする前に、対象 Mac/CPU で外部 Homebrew パスに
依存しない動的ライブラリ構成、移設後の `documents_preview` 実行、ZIP の容量制限、
コード署名と公証を確認します。LibreOffice と Poppler には本体 MIT とは異なる
ライセンスと第三者依存があるため、同梱したバージョンの通知、ソース提供方法、
再配布条件をリリース担当者が確認してください。ファイルの存在検査だけでは
ライセンス適合性やバイナリの移設可能性は証明できません。

既存の出力ファイルは上書きしない。配布 ZIP は dist に置き、リポジトリへ追加しない。
Python と第三者依存のライセンスは runtime 内に保持し、本体の MIT と区別する。

展開した Anywhere Computer フォルダー内で実行する。

```sh
./anywhere chatgpt-setup --state-dir "$HOME/Library/Application Support/Anywhere Computer/chatgpt"
```

上は macOS の例。Windows は `anywhere.cmd`、Linux は `./anywhere` を使い、
利用者の状態保存ディレクトリを明示する。公開 HTTPS、所有者認証、ChatGPT 側の
接続登録は別途必要。GUI インストーラー・OSコード署名・公証は含まない。GitHubの配布物出所証明とは別です。
macOS は実行権限を保つ標準のアーカイブユーティリティまたは ditto で展開する。

配布物自体のエージェント試験は次で再実行できる。信頼済み ZIP を展開したディレクトリを
指定する。実行時に OS 資格情報ストアへのアクセスが必要で、試験専用の一時資格情報を
作成し、終了時に削除する。既存のユーザーエージェントの状態には接続しない。
展開した同梱 Python で直接検査する場合は `-B -I` を指定し、検査前にバイトコードを
書き換えないようにする。

```sh
uv run --offline python scripts/verify_portable.py '/path/to/Anywhere Computer'
```

同梱版の公開 HTTPS 試験には、既存の `scripts/verify_internet.py` を同梱 Python の
`-I` で実行し、`--expected-runtime-root` に同梱 runtime ディレクトリを指定する。
開始前と処理終了時にインタープリターおよび読み込まれた本体モジュールの配置を検査し、
開発ツリーなどからの混入を拒否する。トンネル監督の子 Python も `-I` で起動する。
公開試験には別途 cloudflared と試験専用トンネルが必要で、これらは ZIP に含まれない。

当時の公開試験の結果はローカルの履歴資料に保管しています。
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

Quality CI では三 OS のそれぞれで同梱 ZIP を生成し、日本語・空白パスへ展開した後に
`verify_portable.py --runtime-only` を実行する。このモードはファイル操作と分離した
子 Python の実行を検査し、`native_agent_tested: false` を明示する。CI の資格情報ストア
可用性に依存せず配布構造を検査するためのモードであり、通常モードの実エージェント・
Keychain/Windows Vault/Secret Service 検証を代替しない。成功した ZIP はリポジトリの
Actions artifact として 14 日保持する。ジョブ定義の追加だけでは Windows/Linux の成功
実績としない。実際のジョブ結果は別途確認する。
macOS/Windows の管理 GUI 入り `managed-portable.zip` は別の Quality ジョブで生成・
移設検査し、Actions artifact として 7 日保持する。`main` への push が成功した場合、
その ZIP 自体にも GitHub の出所証明を作成し、同じ Quality workflow・commit・
`refs/heads/main` に結び付くことを検証する。PR や手動実行では証明しない。
出所証明と配布物内 manifest の整合性検査は、GUI の実機操作や公開を証明しない。

ZIP の公開は出力ディレクトリと同じファイルシステム内の staging で全体を完成させてから、
排他的な hard link 作成で行う。既存ファイルを置き換えず、作成失敗時に最終ファイル名で
途中の ZIP を残さない。hard link 非対応のファイルシステムでは失敗を返すため、
その場合は対応するローカルファイルシステム上でビルドする。

検証は信頼できる配布物から展開したフォルダーで行う。manifest に載ったファイルのハッシュ
だけでなく、実ファイル集合との完全一致を検査し、一覧外の Python/.pth/バイトコード等も
拒否する。同梱の起動スクリプト、内部 Python 子プロセス、検証ワーカーは `-B` で
バイトコード生成を抑止するため、通常利用後も同じフォルダーで再検証できる。
外部から Python を直接起動して __pycache__ を生成した場合は検証に失敗する。
検査は余分なファイルを自動削除しない。
manifest 自体は署名されておらず、信頼できる配布元から取得した ZIP の内部整合性検査で
ある。配布元の真正性や、manifest とコードを一緒に差し替える攻撃への保証ではない。
ビルダーは循環コピーを避けるため、ランタイム内のディレクトリ symlink を拒否する。

新しいビルド機では、`uv sync --locked` が成功していても、オフライン再解決に必要な
インデックス情報がキャッシュされているとは限らない。`--allow-downloads` を指定すると、
ビルド時に依存の取得を許可する。`--require-hashes` と固定依存一覧は維持する。
指定しない場合は従来どおりオフラインで、必要情報がなければ失敗する。配布物の実行には
uv もこのダウンロード設定も不要。三 OS の CI では新しい環境のためこのオプションを使う。

通常モードの実エージェント試験には、利用可能で解錠済みの OS 資格情報サービスが必要です。

対話セットアップ完了時には、同じ状態保存先を指定した再開・起動・診断のコマンドを
そのままコピーできる形で表示する。`commands` の JSON は AI 向けで、その後の通常テキストが
人間向けのコピー用。macOS/Linux は POSIX シェル、Windows は PowerShell と明示する。
実行中の Python の絶対パスと `-I` を使用するため、PATH に別の Python があっても選ばない。
配布物を移動した場合は新しい場所からセットアップを再開してコマンドを再生成する。
開始コマンドはフォアグラウンドの `remote-watch` であり、その端末を開いたまま使う。
自動起動登録、公開ルートの作成、資格情報の入力をこの表示だけで実行することはない。

## CI trigger policy

The Quality workflow runs the complete macOS/Windows manager matrix and the
Linux/macOS/Windows runtime matrix for pull requests. Push-triggered runs are
limited to `main`, preserving post-merge verification and main-only provenance
attestations. This avoids duplicate push and pull-request matrices for each PR
commit. Before opening a PR, use the workflow's manual `workflow_dispatch`
entry on the chosen branch. No test steps or platform coverage are removed.
