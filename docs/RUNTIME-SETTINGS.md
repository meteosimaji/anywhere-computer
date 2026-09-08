# 実行設定

`settings_get` と `settings_update` で次の値を取得・変更する。
変更は operation 台帳と同じ SQLite に保存し、次の呼出しから適用する。

- default_shell: 絶対実行パス、または null。terminal_start の shell が省略された場合に使用。
  null は OS の既定選択。実行ファイルの存在・起動可否は実行時に確認する。
- file_read_line_limit: 1〜5000、既定5000。files_read/files_read_many の指定 limit に対する上限。
- file_write_line_limit: 1〜100000、既定10000。files_write に渡すテキストの行数上限。
  文書・バイナリ転送・files_edit の上限ではない。

例: `{"key":"file_read_line_limit","value":500}`。
不正な型・範囲・相対シェルパスは拒否し、以前の値を保持する。
既に実行中の操作に遡って適用する設定ではない。
禁止コマンド、許可ディレクトリ、テレメトリー設定はまだ提供しない。
この設定は OS sandbox ではなく、terminal の実行権限を制限する仕組みでもない。
