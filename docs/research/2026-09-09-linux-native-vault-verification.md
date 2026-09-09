# Linux 同梱版のネイティブ資格情報試験

Ubuntu 24.04.4 ARM64 の既存隔離 VM で、生成済み Linux ZIP を新しい日本語・空白パスへ
展開し、`scripts/verify_portable.py` の通常モードを実行した。前回の runtime-only 試験と
同じ ZIP であり、アーカイブと今回のログ・試験 fixture の SHA-256 は隣の JSON に記録。

試験用ゲスト内だけに `gnome-keyring` と `dbus-x11` を導入した。
`dbus-run-session -- python3 -I /mnt/ac/native-check.py` 内で一時 XDG_DATA_HOME と
XDG_RUNTIME_DIR を作成し、GNOME Keyring の `--daemonize --components=secrets --unlock`
へメモリで生成したパスワードを標準入力のパイプで渡した。パスワードは環境変数、引数、
ログ、平文ファイルには保存しない。これは Linux の実 Secret Service を使う試験であり、
メモリ専用や平文の keyring 代替ではない。

新規展開した 5,346 ファイルの manifest を検査後、同梱 Python から実エージェントを起動。
ファイル書込・読込、別 Python の正規表現検索、接続を閉じた後の同一端末セッションへの
入力と出力、作業中の停止拒否、作業後の正常停止がすべて成功した。試験用の資格情報は
削除した後に再照会して不存在を確認した。一時データを除去し、ゲストを正常に poweroff、
QEMU の終了コード 0 を確認した。

原ログはローカル `output/linux-isolated/serial-native.log`、実行 fixture は
`output/linux-isolated/payload/native-check.py` と `native-check.sh`。
cloud-init の設定スキーマ警告は残ったが、今回の試験スクリプトは終了コード 0 で完了した。
ホストホーム共有・ホストポート転送・ホストの自動起動登録はしていない。

これは隔離ゲスト内のローカル認証・再接続の実測であり、Windows、別の物理 PC、
インターネット越しの Linux 接続、ChatGPT UI、OS 再起動後の自動復帰の証明ではない。
Linux のユーザー環境には利用可能で解錠済みの OS 資格情報サービスが必要である。

GNOME の標準入力による解錠仕様:
https://manpages.ubuntu.com/manpages/jammy/man1/gnome-keyring-daemon.1.html
