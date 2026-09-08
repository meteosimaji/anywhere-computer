# Document inspection

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
