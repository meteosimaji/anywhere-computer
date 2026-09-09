# Linux ARM64 の実測

GitHub Actions が支払い/支出上限の理由で実行前に停止したため、既存 Homebrew QEMU
11.1.0 と HVF で Ubuntu 24.04.4 LTS ARM64 の隔離ゲストを実行した。イメージは
https://cloud-images.ubuntu.com/releases/noble/release/ の公式 SHA256SUMS と照合した。
2 CPU、2 GiB RAM、8 GiB 上限の差分ディスクを使用。ホストホームの共有・ホストへの
ポート転送・ホストの自動起動登録は行っていない。Lima 等の追加インストールは行わなかった。

`build_guest_test_bundle.py` の明示的な許可リストでソース、テスト、必要なスクリプト、
HTML、本体 wheel とチェックサムを ZIP 化し、読み取り専用 ISO で渡した。ゲスト内で
manifest の全ハッシュを照合してからテストした。基準 commit に未コミットのビルダー修正を
加えた試験であり、正確な入力は隣の JSON の bundle SHA-256 に結び付く。

ゲスト内の主要コマンド（uv は試験用 venv に 0.10.8 を導入）:

```sh
uv sync --locked --python 3.12 --managed-python
uv run pytest -q
uv run ruff check src tests scripts
uv run mypy
uv run python scripts/build_portable.py --allow-downloads --output dist/linux-arm64-v3.zip
# ZIP を空白・日本語を含む新規ディレクトリへ展開し、実行権限を復元してから:
uv run python scripts/verify_portable.py 'dist/relocated 日本語/Anywhere Computer' --runtime-only
```

最初の試験では、全体テストが成功した後にオフラインの portable build が失敗した。
`uv sync --locked` による wheel キャッシュだけでは、`uv pip install --offline` の
再解決に必要なインデックス情報が揃うとは限らない。ビルダーへ `--allow-downloads` を
追加し、固定バージョンと `--require-hashes` を保って再試験した。

修正後の実測: **557 passed / 6 skipped、77.01 秒**。Ruff 成功、mypy は 58 source
files 成功。同梱 ZIP の生成・別パスへの展開・ファイル書込/読込・分離子 Python が成功。
これは `--runtime-only` であり、Linux のネイティブ資格情報ストアを使う実エージェントの
受け入れ試験ではない。Windows、別の物理 PC、ChatGPT UI の証明にも代用しない。

生成した ZIP は試験用 FAT ディスクへ書き出して取り出した。macOS でこのディスクの
読み取り専用マウントが失敗したため、GPT/FAT16 の構造を読み取り専用で辿って ZIP を抽出し、
ゲストが出力した SHA-256 と一致することを確認した。ZIP の全 manifest ハッシュと、
本体 58 Python ファイルのソース一致もホストで照合した。試験 VM は正常終了し、
取り出し用のディスク接続も解除した。ISO・停止済み VM・原ログはローカル output に保持。

証拠のハッシュ・範囲・配布物情報は同名の JSON を参照。原ログはローカルの
`output/linux-isolated/serial-v3.log`、取り出し記録は `serial-export.log`。
配布 ZIP は `dist/anywhere-linux-arm64.zip`。これらの大きいローカル成果物は Git に含めない。
