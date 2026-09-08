# 調査成果物の検証記録

2026-09-08 に、以下の 4 検査を一時的な pytest スクリプトで実行し、`4 passed` を確認した。

1. JSON 台帳の 30 ツールと調査本文 DC01–DC30 が完全一致し、名前の重複がない。各 TypeScript 宣言の名前も一致。
2. 固定 upstream commit の `server.ts` 登録 26、switch dispatch、Zod schema の集合を突合。内部 `track_ui_event` の差分と Remote 追加 4 が意図通り。
3. `LICENSE` が GitHub API `GET /licenses/mit` のテンプレートに、年 `2026` と著作者 `めてお (meteosimaji)` を代入したものと一致。
4. README と docs の相対ファイルリンクの対象がすべて存在する（見出しアンカー・外部 URL の検査は含まない）。

検査スクリプト: `/tmp/anywhere-computer-research/test_research_artifacts.py`。公開 upstream の一時 clone と取得したライセンスのテンプレートに依存する調査用検査で、製品 CI ではない。

コマンド: `python3 -m pytest -q /tmp/anywhere-computer-research/test_research_artifacts.py`

製品コードはまだ存在しないため、アプリの型検査・build・MCP 実行・GUI E2E は未実施。上記合格は機能の実装完了を意味しない。
