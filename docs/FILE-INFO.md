# ファイル情報

`files_info` は絶対パスを受け取り、ファイルの内容を開かずに情報を返す。
通常ファイル、ディレクトリ、シンボリックリンク、FIFO、socket、character/block device を識別する。

`size` はバイト数、`modified` と `accessed` は Unix 時刻の秒。
`created` は OS が birth time を提供する場合だけ返し、それ以外は null。
`status_changed` は POSIX の inode 状態変更時刻であり、Windows では null。
ctime を作成時刻に読み替えない。

`permissions` は stat の文字列表現、`mode_octal` は mode bits。
ACL、Windows の実効アクセス権、他ユーザーの権限を計算した値ではない。

通常ファイルの `metadata_subject` は entry。シンボリックリンクでは target になり、
従来の size/modified/directory は参照先の情報を維持する。
リンク自身の情報は `entry`、格納されたリンク文字列は `link_target` に返す。
参照先の `target_state` は resolved/missing/unavailable を区別する。
missing/unavailable では size/modified は null。これらは別々の stat 呼出しによる観測であり、
同時変更中のファイルに対する原子的スナップショットではない。

この API は PDF ページ数や Office のシート数などの形式固有 metadata を提供しない。
文書の内容は `documents_read` の対応範囲を確認すること。

`tests/test_file_info.py` は時刻の意味、mode bits、通常ファイル/ディレクトリ、
参照先削除後のリンク、FIFO を開かないこと、相対パス拒否、engine 経由の公開を検証する。
