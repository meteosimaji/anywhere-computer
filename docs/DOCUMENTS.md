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

`documents_edit_paragraph` replaces one DOCX main-body paragraph selected by its
one-based `paragraph` number from `documents_read`. Supply the exact
`expected_sha256`, `expected_text`, and `new_text`. The tool currently accepts
only a paragraph with one plain text run, with optional paragraph/run formatting;
tabs, line breaks, hyperlinks, fields, and multiple runs are rejected. It
returns the old/new text diff, new hash, and backup ID for `files_restore`.
Before replacement it checks that all other extracted paragraphs are unchanged,
and that every other ZIP package part has identical content. A concurrent file
change fails the atomic hash check. This is a constrained targeted edit, not a
rendered preview or general Word editing. Signed documents are rejected.
If the file hash or expected paragraph text has changed, the operation reports
`document_changed` or `paragraph_changed` with `edit_applied: false`. A late conflict
can leave an internal backup, but the proposed edit is not applied to the target.
Read the document again and reassess the edit with its current hash and text;
do not replay the old request.
Documents whose main Word XML contains markup compatibility attributes or
elements (including `mc:Ignorable`) are also rejected before writing, because
the XML serializer cannot preserve namespace declarations used only by those
constructs.

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

`documents_preview` renders one DOCX page to a PNG using an installed local
LibreOffice (`soffice`), `pdfinfo`, `pdftoppm`, and macOS `sandbox-exec`. Supply
the absolute `path`, the exact `expected_sha256` from `documents_read`, and a
one-based `page` (default 1). Results include the same hash, total page count,
`rendered: true`, `mime_type: image/png`, and base64 PNG data. The workspace UI
uses this for DOCX page previews and leaves extracted text below the image.
If any renderer component is absent, the tool reports an explicit unavailable
error. It does not silently substitute text extraction for a rendered image.

Rendering uses a private temporary copy and LibreOffice profile. Each conversion
and PDF inspection subprocess runs without network access and cannot write outside
that temporary directory. These processes retain local read access for fonts and
renderer libraries, so this is not a full filesystem sandbox. The original hash
is checked again before returning. External
OOXML relationships, fields, active content, and embedded objects are rejected.
Inputs retain the 16 MiB read limit; output is capped at 20 pages, 16 MiB PDF,
and 2 MiB PNG per page. Conversion and rasterization have time and file-size
limits, timed-out child process groups are stopped, and at most two previews
render concurrently per engine. The
preview is a local LibreOffice interpretation, not a Microsoft Office fidelity
guarantee. XLSX and PPTX rendering and a packaged renderer remain future work.

Limits: 16 MiB input, 64 MiB declared expanded ZIP size, 4096 ZIP entries,
4 MiB per XML part, approximately 512 kB per result page, 32768 characters per
text field. Strict OOXML namespaces, encrypted documents, legacy binary Office,
PDF, OCR, rendering within `documents_read`, headers/footers/comments/notes, formatting interpretation,
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
