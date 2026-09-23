# Document inspection

`documents_write` creates a simple DOCX from `text`, or an XLSX from
`rows` (arrays of strings, finite numbers, booleans, nulls, or explicit formula objects)
and optional `sheet_name`. For multiple sheets, use `sheets` instead of `rows`:
`{"Data":[[1,2]],"Summary":[[{"formula":"SUM(Data!A1:B1)"}]]}`.
Names must be unique ignoring case; at most 32 sheets, 100000 total cells and
1000000 total text characters are accepted. Sheet insertion order is retained.
A formula object is `{"formula":"=SUM(A1:A3)"}`.
Strings beginning with `=` remain strings; null cells are omitted. Formula values are not
calculated or cached by this engine. Spreadsheet applications control numeric precision.
Specify `format: docx/xlsx`
and a matching file extension. `mode: create` refuses existing destinations;
`mode: replace` regenerates the entire document and requires `expected_sha256`.
Replacement retains a backup usable by `files_restore`. This is not formatting-preserving
editing. Markdown interpretation, formula calculation and images are not yet implemented.
Generated packages are tested with the internal reader;
Microsoft Office rendering remains unverified.

`documents_edit_cell` makes a targeted edit to one existing plain string cell in an
XLSX workbook. Supply `path`, exact `sheet` name, `cell` in A1 notation,
`old_text`, `new_text`, and the workbook's current `expected_sha256` from
`documents_read`. Set `preview: true` to receive the before/after text diff,
the changed worksheet part, and the predicted result hash without writing.
Repeat with `preview: false` and the same hash to apply the edit. A concurrent
workbook change or a different old cell value rejects the edit. The applied
result includes a backup ID for `files_restore`.

The editor preserves every other ZIP part's uncompressed bytes and keeps the
other cells in the edited worksheet, including their styles and formulas.
It handles existing simple inline or shared string cells only; formulas,
numbers, rich text and empty locations require a separate workflow. Editing
the chosen cell converts a shared string reference into a plain inline string.
The preview is a structured value preview and diff, not an Office-rendered
page. Visual rendering and broad Office editing remain open acceptance gates.

2026-09-09 independent-reader verification also passed using development-only
python-docx and openpyxl: Japanese Word paragraphs, workbook sheet order, numeric/boolean
cells, literal leading-equals strings and explicitly stored formulas. These libraries were
not added to project runtime dependencies. This checks package interpretation, not Office
rendering or formula recalculation.

`documents_read` reads the main-body text of Word OOXML documents, stored Excel
worksheet cells and formulas, and PowerPoint slide paragraphs. It uses Python
ZIP and XML support without Office format libraries. It is a content inspector,
not a renderer or a complete Office implementation.

Arguments: absolute `path`, optional `section` (Excel worksheet name or one-based
PowerPoint slide number), zero-based entry `offset`, and `limit` up to 100.
Results include the original file SHA-256, sections, entry count and next offset.
Excel values are stored strings with the original type/style index; formatted
dates/currency are not inferred. Formula text and cached value are separate;
formulas are never evaluated. Presentation order follows relationships from the
presentation, not the alphabetical order of ZIP filenames. Word table text is
included as paragraphs, without reconstructing the table layout.

The input is never rewritten. External relationships are not dereferenced. ZIP
entry count, expanded package size, per-XML-part size, output page size and field
length are bounded. Truncated text is marked. XML DTD/entity declarations are
rejected and only UTF-8 OOXML parts are currently accepted. Named pipes are
rejected before a read can wait indefinitely.

Limits: 16 MiB input, 64 MiB declared expanded ZIP size, 4096 ZIP entries,
4 MiB per XML part, approximately 512 kB per result page, 32768 characters per
text field. Strict OOXML namespaces, encrypted documents, legacy binary Office,
PDF, OCR, rendering, headers/footers/comments/notes, formatting interpretation,
charts and document editing are not implemented in this reader. A workbook
without a selected worksheet defaults to its first listed sheet; a non-worksheet
sheet type is reported as unsupported.

Tests use independently constructed fixtures for Word paragraphs/tables,
Excel shared and inline strings with cached formulas, PowerPoint relationship
order, unchanged input bytes, malformed relationships, DTD rejection and size
limits. These are not Microsoft Office UI or arbitrary-document compatibility
tests. Full Office compatibility remains an unfinished product requirement.

Format references:
- https://learn.microsoft.com/en-us/office/open-xml/spreadsheet/working-with-the-shared-string-table
- https://learn.microsoft.com/ja-jp/dotnet/api/documentformat.openxml.spreadsheet.cell
- https://learn.microsoft.com/en-us/office/open-xml/word/how-to-open-and-add-text-to-a-word-processing-document
# Excel の範囲指定

`documents_read` の `cell_range` に `B2:D10` や `$B$2:$D$10` を指定すると、選択した
シートの矩形範囲内に保存されているセルだけを返す。単一セルも指定可能。
範囲で絞り込んでから offset/limit を適用する。空セルを二次元配列に補完する機能ではない。
シート名は section で指定し、範囲に `Sheet!` を混ぜない。
逆向き範囲、不正なセル番地、Word/PowerPoint への cell_range は拒否する。
数式は保存文字列を返し、再計算・書換えは行わない。
