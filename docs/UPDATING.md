# 更新と接続の引き継ぎ

2026-09-19時点の公開版はPython `0.1.0b1` / Codex Plugin `0.1.0-beta.1`です。
[Beta 1の配布物](https://github.com/meteosimaji/anywhere-computer/releases/tag/v0.1.0b1)は
プレリリースです。このチェックアウトの `0.2.0a11` / `0.2.0-alpha.11` は開発版で、公開betaとは異なります。
正式stableリリースと一般向け自動更新の受け入れは未完了です。

## 公開beta／alphaを手動で更新する

更新したい版を取得し、現在の状態保存先を引き継ぎます。実行フォルダーと、
資格情報・設定・操作履歴の保存先は別です。新しい空の状態保存先を指定すると、
既存の設定を引き継ぐ更新にはなりません。

1. 現在使っているローカルエンジンとHTTPサービスの状態保存先を確認する。
2. 新しい版を別の実行フォルダーに用意し、その版の依存を準備する。
3. 端末・Pluginセッションなどの作業を終えてから、新しい版の `anywhere start` を
   同じローカル状態保存先に対して実行する。
4. OSの常駐登録も更新する場合は、新しい版で同じHTTP設定先に対して
   `autostart-upgrade`、続いて `autostart-start` を実行する。
5. `status`と実際のMCP接続で版・応答を確認し、必要なら既知の操作IDの結果を回収する。

ソースからの起動では `uv run --locked anywhere`、同梱配布物ではmacOS/Linuxの
`./anywhere`、Windowsの `anywhere.cmd` を使います。以下はソース版の例です。
山括弧の部分を実際の保存先に置き換えてください。

```sh
uv sync --locked --python 3.12
uv run --locked anywhere start --state-dir <既存のローカル状態保存先>
uv run --locked anywhere status --state-dir <既存のローカル状態保存先>
```

常駐登録を使っている場合だけ、同じ版から次を実行します。

```sh
uv run --locked anywhere autostart-upgrade --state-dir <既存のHTTP設定保存先>
uv run --locked anywhere autostart-start --state-dir <既存のHTTP設定保存先>
uv run --locked anywhere autostart-status --state-dir <既存のHTTP設定保存先>
uv run --locked anywhere remote-doctor --state-dir <既存のHTTP設定保存先> --probe-public
```

PATHに接続子がない環境では、`autostart-upgrade`に
`--connector <cloudflaredの絶対パス>`も指定します。起動登録は実行中のPythonを
保存するため、新しい版から実行してください。選択中の実行フォルダーは削除・移動
しないでください。古い版のコードへ戻すことと、古い台帳へ戻すことは別であり、
台帳の巻き戻しによる復旧はサポートしません。

## ChatとCodexが同じ版を使う条件

共通エンジンへ移行済みなら、HTTP入口とローカル入口は同じエンジンへ接続します。
通常接続では互換APIの既存エンジンを再利用し、選択した実行版を起動先に保持します。
互換性がない場合は、接続だけで稼働版を強制置換しません。

古いalphaでは、HTTPとローカルが別エンジンだったり、古いクライアントが自分の版を
起動し直したりする挙動があります。共通化は通常更新とは別の一度きりの移行です。
`engine-unify`は両サービスの停止を前提とし、元のデータを残して新しい保存先へ移行
します。既に共通化した環境で繰り返す手順ではありません。
当時の共通化の検証記録はローカルの履歴資料に保管しています。

macOSでは更新後も既存のHTTP認証・URLを使って接続し、旧操作結果を回収できました。
一方、Pluginを更新しただけでは、既に起動している全CodexタスクのMCPプロセスが
自動で再読込されるとは限りません。パッケージの導入成功、稼働版の切替、各入口の
再接続をそれぞれ確認してください。

HTTP設定保存先と、実際の操作を実行する共通エンジンの状態保存先が異なる場合が
あります。HTTPサービスだけを新しい版にしても、共通エンジンの更新完了には
なりません。更新後はChatGPTとCodexの各接続から `computer_status` を実行し、
`version` に加えて `runtime_id` と `instance_id` を照合してください。同じ共通
エンジンを使う構成なら、両方の識別値が一致する必要があります。別PC同士では
instance IDが異なるのが正常です。同じalpha版の修正でもruntime IDは変わるため、
版番号だけで更新済みと判定しないでください。

2026-09-13の実検証では、HTTP設定先だけ更新され、共通エンジンが旧ビルドのまま
残っている事例を確認しました。実際の共通エンジンの状態保存先に対して、新しい
インストールの `start` を実行し、両入口の応答で更新を確認しています。
当時の検証記録はローカルの履歴資料に保管しています。

## stable向け更新コマンド

以下は公開beta 1にも含まれます。alpha／betaのプレリリースは対象にせず、正式stable候補だけを対象にします。

```sh
uv run --locked anywhere update --state-dir <ローカル状態保存先> --verifier <ghの絶対パス>
```

1回の処理で、未完了候補があれば再開し、なければ稼働版より新しいstableを探します。
新規候補には、同じcommitのQuality CI成功、対象OS／CPU、配布物ハッシュ、公式ghに
よる出所証明、API／台帳互換性、隔離展開後のPython起動前検査を要求します。

| 結果 | 意味 |
|---|---|
| `no_stable_release` | 正式stable候補がない。版は変更していない |
| `current` | 対象と同じ実装が稼働中、または新しいstableがない |
| `waiting` | 実行中の作業がある。候補準備済みなら保存して再利用する |
| `applied` | 切替後のエンジン応答で版とruntime IDを確認した |
| エラー／応答喪失 | 適用成功とは断定しない。候補と中断記録を残す |

通常接続は記録済みの中断更新を起動前に復旧します。候補なしの中断記録、異なる
実行ファイル、壊れた記録を推測で置き換えません。操作結果は既存の台帳を引き継ぎます。

更新は既定で手動です。共通エンジンを使う常駐HTTPサービスで自動更新を希望する場合だけ、
次のコマンドをHTTP設定保存先に対して実行し、サービスを再起動します。

```sh
uv run --locked anywhere auto-update-enable --state-dir <既存のHTTP設定保存先>
uv run --locked anywhere autostart-upgrade --state-dir <既存のHTTP設定保存先>
uv run --locked anywhere autostart-start --state-dir <既存のHTTP設定保存先>
```

無効化には `auto-update-disable` を同じ保存先に対して実行し、同様に常駐登録を更新して起動します。
常駐登録の更新は実行中の接続を切断するため、作業終了後に選択中の版から行ってください。
`autostart-start` 単独では稼働中のサービスを再起動しません。
設定は再起動時に読み込まれます。無効化コマンドだけで実行中の更新を中断しません。
既存のHTTP URL・認証設定は変更しません。設定が存在しない、または壊れている場合は
自動更新を開始しません。ローカルだけの起動では自動更新監視は動きません。

有効時は通常1時間、作業待機・失敗時60秒でstableを再確認します。ghがない場合は
検証を省略せず `verifier_unavailable` を記録します。更新前には実行中の作業を確認します。
正式stable候補はまだありません。自動更新を本番で実運用したという意味ではありません。
検証には別途ghが必要です。正式stableを使った更新の受け入れは残っています。

## 証拠と未確認事項

- 公開alphaのmacOS常駐更新・再接続: ローカルの履歴資料に保管した更新検証記録。
- 開発中の取得・検証・中断復旧・監視: ローカルの履歴資料に保管した実装記録。
- 配布ZIPの構造・CIと実機の違い: [配布ガイド](PORTABLE.md)。

合成候補による中断復旧、CI配布物の出所証明検証、正式stableの実機更新は異なる検証
です。前二者は確認済みですが、正式stableによる一連の更新成功はまだ確認していません。


### Beta 1 and development CLI: verifier discovery

Beta 1 and development builds allow `anywhere update` to find an installed GitHub CLI (`gh`)
on the current PATH. `--verifier` still takes precedence and must be absolute.
An unavailable verifier stops before an update begins; nothing is downloaded or
installed to satisfy this prerequisite automatically. The existing provenance
checks, stable-only selection, idle-work protection and manual-update default are
unchanged. Published alpha9 still requires the explicit verifier argument shown
above. Service PATH may differ from an interactive terminal.
