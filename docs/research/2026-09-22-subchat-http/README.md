# 2026-09-22 subchat HTTP監査の証拠と再現手順

[調査の統合報告](../../SUBCHAT-HTTP-AUDIT-2026-09-22.ja.md)に対応する、日付付きの研究記録。基準ソースは`3b53ef2805032b1169b51e1e46f461d8bedd6593`である。runtimeの能力設定や、常に成功すべき標準テストの追加ではない。

## ファイルの意味

| ファイル | 内容と限界 |
| --- | --- |
| [evidence.json](evidence.json) | 異なる実験の結果を分けた要約。監査ログから抽出した観測値も含む。生ログのバイト同一コピーではない |
| [manifest.json](manifest.json) | 原本名・原本SHA-256・公開SHA-256・変換内容。コピーまたは投影した11ファイルが対象。新規作成のこのREADMEとevidence.jsonは原本コピーではない |
| [full-tests.log](full-tests.log) | 基準ソースの標準全体試験。1,652 passed、19 skipped |
| [ci-baseline.json](ci-baseline.json) | 基準ソースのpush CI run 35710942533、5ジョブ成功。将来の文書commitのCI結果ではない |
| [publication-validation.json](publication-validation.json) | 文書公開前に保存予定のprobeを別directoryで追試した結果。package／runtime確認の11試験は成功。監査は1 passed／4 failedを再確認。新規作成の記録で、manifestの原本コピーには含めない |
| [http-creation-result.json](http-creation-result.json) | Keychainの認証アクセス拒否でHTTP前に停止。新規生成POSTは0。認証拒否とprovider 403を区別する |
| [diagnostic-offline-tests.log](diagnostic-offline-tests.log) | 上記診断の8件のオフライン試験。認証取得コード自体は公開対象外 |
| [streaming-result.json](streaming-result.json) | localhost SSEでのPlaywright通常POSTとHTTPX streamの受信タイミング。実Chat受理ではない |
| [ledger-lifecycle-result.json](ledger-lifecycle-result.json) | 二つ目のLedger初期化により一般operationがrunningからunknownへ変わった合成試験。絶対sourceパスだけを相対化 |
| [request-validator-cases.json](request-validator-cases.json) | generation_inputの16境界ケース。絶対sourceパスだけを相対化。合格はprovider受理を意味しない |
| [probes](probes) | 下記4本の合成／localhost再現コード。原本からバイト同一で保存 |

## 保存した再現コード

- [test_audit_regressions.py](probes/test_audit_regressions.py)：回収枠、watchと外側idle、GUIの不整合応答。合成provider・実stdio・隔離SQLiteのみ。実Chatやデスクトップへ入力しない。
- [streaming_probe.py](probes/streaming_probe.py)：127.0.0.1に一時HTTPサーバーを置く。ブラウザー起動関数は禁止し、providerには接続しない。
- [ledger_lifecycle_probe.py](probes/ledger_lifecycle_probe.py)：一時directoryの合成DBだけを使う。製品の稼働中DBは開かない。
- [contract_boundary_probe.py](probes/contract_boundary_probe.py)：実際の要求検査器へ合成JSONを渡す。socket.connectを禁止し、ネットワーク接続試行0を確認する。

これは調査時のコードを保存したものであり、保護回避、認証取得、通常Chat送信の実装ではない。8件の診断試験と、ここにある16ケースの境界試験は別の集合である。

## 追試の前提

Python 3.12、基準ソースの`uv.lock`に対応する開発依存関係を利用する。元の試験はmacOS、Playwright 1.58.0、HTTPX 0.28.1で実行した。別OSや別版で同じ結果を保証するものではない。下記では、確認対象checkoutのrootをカレントdirectoryとし、その`.venv/bin/python`を使う。新しい結果を既存の証拠ファイルへ上書きしない。

```sh
ROOT="$(pwd -P)"
PY="$ROOT/.venv/bin/python"
EVIDENCE="$ROOT/docs/research/2026-09-22-subchat-http"
RUN="$(mktemp -d "${TMPDIR:-/tmp}/ac-http-evidence.XXXXXX")"
cp "$EVIDENCE/probes/contract_boundary_probe.py" "$RUN/"
cp "$EVIDENCE/probes/ledger_lifecycle_probe.py" "$RUN/"
cp "$EVIDENCE/probes/streaming_probe.py" "$RUN/"
cp "$EVIDENCE/probes/test_audit_regressions.py" "$RUN/"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT/src:$ROOT/tests"
"$PY" "$RUN/contract_boundary_probe.py"
"$PY" "$RUN/ledger_lifecycle_probe.py"
"$PY" "$RUN/streaming_probe.py"
"$PY" -m pytest -q -s -c "$ROOT/pyproject.toml" "$RUN/test_audit_regressions.py"
```

最初の3本は基準版でexit 0になった。最後の監査試験は、未修正の要求をassertするため、`3b53ef2`では1 passed／4 failed、exit 1になることを確認している。初期main `e2100eb`では5 failedだった。標準CIへそのまま加えて「すべて成功するはず」と解釈しない。修正する場合は対象ケースを製品の回帰試験へ移し、既存のguardを弱めずに成功することを確認する。

probesは生成結果JSONを自身のdirectoryへ新規作成する。同じRUNで再実行すると既存ファイルの上書きを拒否するので、新しいRUNを作る。実行に必要な依存関係がない場合は環境の失敗であり、不具合の再現成功とは扱わない。原本probeが出力する実行先パスはローカル診断用で、新しい結果を公開するときは再度選別する。

文書公開時に追試した場合の結果は、基準版の歴史的ログとは別に報告する。既存の標準全体試験の結果を、新しいcommitで再実行したことにはしない。

## 来歴・公開範囲

manifestの`byte-identical`は原本と公開ファイルのbytesが同じという意味。パス相対化したJSONは、その変更を明示して原本・公開の両digestを記録した。`evidence.json`の監査観測は、原本の先頭に出力されたJSON行から抽出し、個人directoryを含むtracebackを載せずに保存した。

認証情報、復号用コード、browser profile、実会話本文、Keychainの内容、個人directoryの一覧は含まない。認証拒否を示す非秘密の状態・OSStatusと試行回数だけを公開する。基準CIの過去の成功、監査の期待違反、providerの実受理を混同しない。
