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
別 Python プロセスを使う正規表現検索が成功した。Windows/Linux の同梱配布物は未検証。

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
