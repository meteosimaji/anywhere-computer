# 任意コード実行の監査と修正

監査対象は fb733ce3234ebfd28399eddd2beaaae2e108d785 と当時の文書検索作業差分。
Codex Security scan: aed50ba6-08a9-4f45-a964-03fa4378437d。
49本の実行時 Python モジュールを親と独立レビューで確認した。
リポジトリ全体の文書・全テスト・外部依存・各OS実運用を網羅した証明ではない。

## 確認した問題

CWE-427: 内部 Python 子プロセスの `-m` 起動が作業ディレクトリを import 検索に含めた。
未信頼 cwd に同名パッケージを置ける場合、正規表現検索に terminal grant がなくても
検索子プロセスの起動時にそのコードが実行される。無害な一時マーカーファイルの作成で
実際の regex_line_numbers 経路を再現した。影響はエージェントOSアカウントのコード実行。
同じ原因はローカルagent起動、HTTP監視、remote監視・connector runner起動にも存在した。
通常のプラグイン cwd は専用の信頼済みディレクトリであり、無認証のインターネットRCEを
再現したという意味ではない。前提付きのため監査での重大度は medium。

## 修正

runtime_launch.python_module_command に内部起動を集約し、Python `-I` を付ける。
cwd、PYTHONPATH/PYTHONHOME、user-site の暗黙importに依存せず、選択したインタープリターの
インストール済みパッケージを使用する。venvのインタープリターシンボリックリンクは維持する。
未インストールのソースを PYTHONPATH だけで差し込む起動にはフォールバックしない。
開発は uv sync によるインストール済み環境、配布は wheel のインストールを前提とする。
これは Python import の隔離であり、OS sandbox や端末コマンドの無害化ではない。

新規の3OS自動起動定義にも -I を適用する。旧receiptは元の定義を再構成して
状態確認・所有権検査付き解除を可能にするが、そのまま再有効化しない。
旧登録がある場合は autostart-uninstall 後に autostart-install で作り直す。
資格情報は保持し、今回ユーザー環境のOS登録は変更していない。

settings_update の default_shell はエンジン共有設定であり、他クライアントの後続端末起動に
影響する。既存の同一所有者・非隔離契約では独立した認可突破として数えないが、
ツール説明・openWorldHint・設定文書で実行に関係する権限として明示した。
ファイル書込とterminal操作は元々OSアカウントの強い権限であり、認可セットはsandboxではない。

## 検証範囲

修正後の全体試験は **470 passed / 5 skipped**。Ruff、mypy（通常と win32 型検査）、
plugin 構造検証、同梱wheel全50 Pythonファイルとソース/checksum一致が成功。
別の一時インストール環境でも、偽packageを置いたcwdから正規wheelのCLIが起動し、
偽moduleのマーカーは作られなかった。依存の追加はない。
復旧試験の模擬ストアは専用venvのtrusted siteに配置し、実際の -I 起動を維持したまま
子プロセスクラッシュ復帰と所有プロセス喪失時の後片付けを検証した。

- 修正前: 一時cwdの偽workerがマーカーを作成することを実測。
- 修正後: 偽packageとsitecustomize、PYTHONPATH、無効PYTHONHOMEを与えても、
  正規workerが一致行2を返し、両CLI入口が正常に起動し、マーカーが作られない回帰試験。
- 新旧自動起動receiptの再有効化拒否・解除・再登録、venvパス維持の試験。
- 既存のHTTP/OAuth/PKCE/失効、ファイル、文書、転送、プロセスと端末試験。

Windows/Linuxの実機RCE再現、公開二端末経路、OSサービス登録の実変更は今回未実施。
ローカル試験の成功をこれらの証明として扱わない。監査の確定reportは修正前のスナップショットを
記録した不変成果物であり、修正後の状態は本ノートと回帰試験で区別する。
