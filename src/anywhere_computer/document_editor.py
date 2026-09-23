"""Conservative, hash-conditional edits of existing OOXML worksheet string cells."""

import io
import zipfile
from xml.etree import ElementTree as ET

from pydantic import JsonValue

from .document_writer import checked_text, xml_bytes
from .documents import RID, SHEET, OfficePackage, cell_position
from .files import MAX_READ_BYTES, Files, absolute_path, read_bytes, sha256
from .models import EditSpreadsheetCell


def _plain_text(cell: ET.Element, shared: list[str | None]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find(SHEET + "is")
        if (inline is None or len(cell) != 1 or len(inline) != 1
                or inline[0].tag != SHEET + "t"):
            raise ValueError("Only plain string cells can be edited")
        return inline[0].text or ""
    if kind == "s":
        value = cell.find(SHEET + "v")
        if value is None or len(cell) != 1:
            raise ValueError("Only plain string cells can be edited")
        try:
            index = int(value.text or "")
            if not 0 <= index < len(shared):
                raise ValueError("Invalid shared string reference")
            text = shared[index]
            if text is None:
                raise ValueError("Only plain string cells can be edited")
            return text
        except ValueError as error:
            raise ValueError("Invalid shared string reference") from error
    raise ValueError("Only existing plain string cells can be edited")


def edit_spreadsheet_cell(files: Files, args: EditSpreadsheetCell) -> dict[str, JsonValue]:
    path = absolute_path(args.path)
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Cell editing requires an XLSX path")
    if path.is_symlink():
        raise ValueError("Edit the real workbook path, not a symbolic link")
    original = read_bytes(path)
    if sha256(original) != args.expected_sha256:
        raise ValueError("Workbook changed; read it again")
    if args.old_text == args.new_text:
        raise ValueError("New cell text must differ from old text")
    checked_text(args.new_text)
    wanted = cell_position(args.cell)
    package = OfficePackage(original)
    try:
        main = package.main_part()
        workbook = package.xml(main)
        if workbook.tag != SHEET + "workbook":
            raise ValueError("Package is not an XLSX workbook")
        sheets = workbook.find(SHEET + "sheets")
        if sheets is None:
            raise ValueError("Workbook has no sheets")
        selected = [sheet for sheet in sheets if sheet.get("name") == args.sheet]
        if len(selected) != 1:
            raise ValueError("Worksheet was not found or is ambiguous")
        relations = package.relationships(main)
        relation = relations.get(selected[0].get(RID, ""))
        if relation is None or not relation[0].endswith("/worksheet"):
            raise ValueError("Selected sheet is not a worksheet")
        part = relation[1]
        worksheet = package.xml(part)
        data = worksheet.find(SHEET + "sheetData")
        if data is None:
            raise ValueError("Worksheet has no cells")
        matches = [cell for row in data for cell in row.findall(SHEET + "c")
                   if cell_position(cell.get("r", "")) == wanted]
        if len(matches) != 1:
            raise ValueError("Cell was not found or is ambiguous")
        shared: list[str | None] = []
        if matches[0].get("t") == "s":
            shared_parts = [target for kind, target in relations.values()
                            if kind.endswith("/sharedStrings")]
            if len(shared_parts) != 1:
                raise ValueError("Shared string table is missing or ambiguous")
            for item in package.xml(shared_parts[0]).findall(SHEET + "si"):
                if len(item) != 1 or item[0].tag != SHEET + "t":
                    shared.append(None)
                else:
                    shared.append(item[0].text or "")
        cell = matches[0]
        before = _plain_text(cell, shared)
        if before != args.old_text:
            raise ValueError("Cell text changed; no edit was applied")
        cell.set("t", "inlineStr")
        for child in list(cell):
            cell.remove(child)
        inline = ET.SubElement(cell, SHEET + "is")
        text = ET.SubElement(inline, SHEET + "t",
                             {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
        text.text = args.new_text
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.comment = package.archive.comment
            for info in package.archive.infolist():
                archive.writestr(info, xml_bytes(worksheet) if info.filename == part
                                 else package.archive.read(info))
        content = output.getvalue()
    finally:
        package.archive.close()
    if len(content) > MAX_READ_BYTES:
        raise ValueError("Edited workbook exceeds the 16 MiB file limit")
    result: dict[str, JsonValue] = {
        "path": str(path), "sheet": args.sheet, "cell": args.cell.upper(),
        "before": before, "after": args.new_text,
        "diff": {"old": before, "new": args.new_text},
        "original_sha256": args.expected_sha256,
        "sha256": sha256(content), "preview": args.preview,
        "changed_part": part,
    }
    if args.preview:
        return result
    written = files._write_bytes(args.path, content, "replace", args.expected_sha256)
    return {**result, **written}
