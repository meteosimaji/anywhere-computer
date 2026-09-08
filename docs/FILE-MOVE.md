# 排他的な移動

files_move は同一ファイルシステム上のファイル・フォルダ・シンボリックリンクを移動する。
移動先が存在すれば失敗し、空フォルダであっても上書きしない。事前の exists 判定だけに依存しない。

Linux は renameat2(RENAME_NOREPLACE)、macOS は renamex_np(RENAME_EXCL)、Windows は
os.rename を使う小さい adapter に分ける。共通APIと他の処理は共有する。
API/ファイルシステムが排他的renameを提供しない場合はエラーとし、上書き可能なrenameへ変更しない。
別ファイルシステムへのコピーと削除は自動実行しない。外部プロセスによる親パスの同時変更に
対する完全な隔離や、移動した対象の永続的な同一性保証ではない。

参照: [Linux rename](https://man7.org/linux/man-pages/man2/renameat2.2.html)、
[Apple exclusive rename](https://developer.apple.com/documentation/foundation/urlresourcekey/volumesupportsexclusiverenamingkey?language=objc)。
ローカル試験は tests/test_move_native.py の通常移動・衝突・同時移動・リンクを使う。
他OSでの実呼出しは三OS CIの成功を確認してから検証済みとする。
