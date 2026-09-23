"""Small OOXML document generation using only ZIP and XML from the standard library."""

import io
import math
import zipfile
from typing import cast
from xml.etree import ElementTree as ET

from pydantic import JsonValue

from .documents import REL, SHEET, WORD, OfficePackage, word_paragraphs
from .files import Files, absolute_path, read_bytes, sha256
from .models import EditDocumentParagraph, FormulaCell, SpreadsheetValue, WriteDocument

CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MARKUP_COMPATIBILITY = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def write_document(files: Files, args: WriteDocument) -> dict[str, JsonValue]:
    if absolute_path(args.path).suffix.lower() != "." + args.format:
        raise ValueError("Document extension must match the requested format")
    if args.format == "docx":
        if args.text is None or args.rows is not None or args.sheets is not None:
            raise ValueError("Word generation requires text and does not accept rows")
        content = create_word(args.text)
    else:
        if args.text is not None or (args.rows is None) == (args.sheets is None):
            raise ValueError("Workbook generation requires exactly one of rows or sheets")
        content = (create_workbooks(args.sheets) if args.sheets is not None
                   else create_workbook(args.rows or [], args.sheet_name))
    return files._write_bytes(args.path, content, args.mode, args.expected_sha256)


def edit_document_paragraph(files: Files, args: EditDocumentParagraph) -> dict[str, JsonValue]:
    """Edit one plain DOCX paragraph while retaining other package parts and a backup."""
    path = absolute_path(args.path)
    if path.suffix.lower() != ".docx":
        raise ValueError("Paragraph editing requires a DOCX file")
    if any(char in args.new_text for char in "\r\n\t"):
        raise ValueError("Use a single plain-text paragraph without tabs or line breaks")
    checked_text(args.new_text)
    original = read_bytes(path)
    if sha256(original) != args.expected_sha256:
        raise ValueError("Document changed; read it again")
    package = OfficePackage(original)
    try:
        if any(name.casefold().startswith("_xmlsignatures/")
               for name in package.archive.namelist()):
            raise ValueError("Signed documents cannot be edited by this tool")
        part = package.main_part()
        root = package.xml(part)
        if root.tag != WORD + "document":
            raise ValueError("Paragraph editing requires a Word document")
        # ElementTree drops namespace declarations used only in mc:Ignorable's
        # prefix list, which can make the rewritten document invalid for Word.
        if any(
            element.tag.startswith(MARKUP_COMPATIBILITY)
            or any(name.startswith(MARKUP_COMPATIBILITY) for name in element.attrib)
            for element in root.iter()
        ):
            raise ValueError("Markup compatibility content cannot be edited by this tool")
        body = root.find(WORD + "body")
        if body is None:
            raise ValueError("Word document has no body")
        before = word_paragraphs(body, package.budget)
        if args.paragraph > len(before):
            raise ValueError("Paragraph was not found")
        entry = before[args.paragraph - 1]
        assert isinstance(entry, dict)
        if entry["text"] != args.expected_text:
            raise ValueError("Paragraph changed; read it again")
        if args.expected_text == args.new_text:
            raise ValueError("New paragraph text matches existing text")
        paragraph = list(body.iter(WORD + "p"))[args.paragraph - 1]
        runs = [child for child in paragraph if child.tag == WORD + "r"]
        if (any(child.tag not in {WORD + "pPr", WORD + "r"} for child in paragraph)
                or len(runs) != 1):
            raise ValueError("Only a single plain-text run can be edited")
        run = runs[0]
        text_nodes = [child for child in run if child.tag == WORD + "t"]
        if (any(child.tag not in {WORD + "rPr", WORD + "t"} for child in run)
                or len(text_nodes) != 1):
            raise ValueError("Only a single plain-text run can be edited")
        text_nodes[0].text = args.new_text
        text_nodes[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        after = word_paragraphs(body, package.budget)
        expected_after = [dict(item) for item in before if isinstance(item, dict)]
        expected_after[args.paragraph - 1]["text"] = args.new_text
        if after != expected_after:
            raise ValueError("Paragraph preservation check failed")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for info in package.archive.infolist():
                payload = xml_bytes(root) if info.filename == part else package.archive.read(info)
                archive.writestr(info, payload)
        edited = output.getvalue()
        with zipfile.ZipFile(io.BytesIO(edited)) as check:
            for info in package.archive.infolist():
                if (info.filename != part
                        and check.read(info.filename) != package.archive.read(info)):
                    raise ValueError("Document part preservation check failed")
        result = files._write_bytes(args.path, edited, "replace", args.expected_sha256)
        return {**result, "format": "docx", "changed_part": part,
                "preserved_parts": len(package.archive.namelist()) - 1,
                "diff": {"paragraph": args.paragraph, "before": args.expected_text,
                         "after": args.new_text}}
    except (KeyError, zipfile.BadZipFile, ET.ParseError) as error:
        raise ValueError("Office package is malformed or references an unavailable part") from error
    finally:
        package.archive.close()


def xml_bytes(root: ET.Element) -> bytes:
    return cast(bytes, ET.tostring(root, encoding="utf-8", xml_declaration=True))


def package_document(main: str, content_type: str, parts: dict[str, bytes]) -> bytes:
    # OPC package metadata uses default namespaces. Prefixing these roots as
    # ns0 produces files that LibreOffice cannot open, despite valid XML.
    types = ET.Element("Types", xmlns=CONTENT_TYPES)
    ET.SubElement(
        types,
        "Default",
        Extension="rels",
        ContentType="application/vnd.openxmlformats-package.relationships+xml",
    )
    ET.SubElement(
        types, "Default", Extension="xml", ContentType="application/xml"
    )
    ET.SubElement(
        types, "Override", PartName="/" + main, ContentType=content_type
    )
    for name in parts:
        if name.startswith("xl/worksheets/"):
            ET.SubElement(
                types,
                "Override",
                PartName="/" + name,
                ContentType="application/vnd.openxmlformats-officedocument."
                "spreadsheetml.worksheet+xml",
            )
    relationships = ET.Element("Relationships", xmlns=REL)
    ET.SubElement(
        relationships,
        "Relationship",
        Id="document",
        Type=OFFICE_REL + "/officeDocument",
        Target=main,
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", xml_bytes(types))
        archive.writestr("_rels/.rels", xml_bytes(relationships))
        for name, data in parts.items():
            archive.writestr(name, data)
    return output.getvalue()


def checked_text(value: str) -> str:
    if any(not (char in "\n\r\t" or 0x20 <= ord(char) <= 0xD7FF
                or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF)
           for char in value):
        raise ValueError("Document text contains unsupported XML control characters")
    if len(value) > 1000000:
        raise ValueError("Document text exceeds the limit")
    return value


def create_word(text: str) -> bytes:
    root = ET.Element(WORD + "document")
    body = ET.SubElement(root, WORD + "body")
    for line in checked_text(text).split("\n"):
        paragraph = ET.SubElement(body, WORD + "p")
        run = ET.SubElement(paragraph, WORD + "r")
        for index, fragment in enumerate(line.rstrip("\r").split("\t")):
            if index:
                ET.SubElement(run, WORD + "tab")
            node = ET.SubElement(
                run, WORD + "t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}
            )
            node.text = fragment
    return package_document(
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        {"word/document.xml": xml_bytes(root)},
    )


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def create_workbooks(sheets: dict[str, list[list[SpreadsheetValue]]]) -> bytes:
    if not 1 <= len(sheets) <= 32:
        raise ValueError("Use between 1 and 32 worksheets")
    if len({name.casefold() for name in sheets}) != len(sheets):
        raise ValueError("Worksheet names must be unique ignoring case")
    if sum(len(row) for rows in sheets.values() for row in rows) > 100000:
        raise ValueError("Workbook exceeds cell limits")
    if sum(len(value.formula if isinstance(value, FormulaCell) else str(value))
           for rows in sheets.values() for row in rows for value in row) > 1000000:
        raise ValueError("Workbook text exceeds the limit")
    workbook = ET.Element(SHEET + "workbook")
    sheet_list = ET.SubElement(workbook, SHEET + "sheets")
    relationships = ET.Element(f"{{{REL}}}Relationships")
    parts: dict[str, bytes] = {}
    for index, (name, rows) in enumerate(sheets.items(), 1):
        # Reuse the same worksheet validator and serializer for both APIs.
        single = create_workbook(rows, name)
        part = f"xl/worksheets/sheet{index}.xml"
        with zipfile.ZipFile(io.BytesIO(single)) as archive:
            parts[part] = archive.read("xl/worksheets/sheet1.xml")
        identity = f"sheet{index}"
        ET.SubElement(sheet_list, SHEET + "sheet", name=name, sheetId=str(index),
                      attrib={f"{{{OFFICE_REL}}}id": identity})
        ET.SubElement(relationships, f"{{{REL}}}Relationship", Id=identity,
                      Type=OFFICE_REL + "/worksheet", Target=f"worksheets/sheet{index}.xml")
    parts["xl/workbook.xml"] = xml_bytes(workbook)
    parts["xl/_rels/workbook.xml.rels"] = xml_bytes(relationships)
    return package_document("xl/workbook.xml",
                            "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet.main+xml", parts)


def create_workbook(rows: list[list[SpreadsheetValue]], sheet_name: str = "Sheet1") -> bytes:
    if (
        not 1 <= len(sheet_name) <= 31
        or any(c in sheet_name for c in "[]:*?/\\")
        or sheet_name.startswith("'")
        or sheet_name.endswith("'")
    ):
        raise ValueError("Invalid worksheet name")
    checked_text(sheet_name)
    if len(rows) > 10000 or sum(len(row) for row in rows) > 100000:
        raise ValueError("Worksheet exceeds cell limits")
    if sum(len(cell.formula if isinstance(cell, FormulaCell) else str(cell))
           for row in rows for cell in row) > 1000000:
        raise ValueError("Worksheet text exceeds the limit")
    workbook = ET.Element(SHEET + "workbook")
    sheets = ET.SubElement(workbook, SHEET + "sheets")
    ET.SubElement(
        sheets,
        SHEET + "sheet",
        name=sheet_name,
        sheetId="1",
        attrib={f"{{{OFFICE_REL}}}id": "sheet1"},
    )
    relationships = ET.Element(f"{{{REL}}}Relationships")
    ET.SubElement(
        relationships,
        f"{{{REL}}}Relationship",
        Id="sheet1",
        Type=OFFICE_REL + "/worksheet",
        Target="worksheets/sheet1.xml",
    )
    worksheet = ET.Element(SHEET + "worksheet")
    data = ET.SubElement(worksheet, SHEET + "sheetData")
    for number, cells in enumerate(rows, 1):
        if len(cells) > 16384:
            raise ValueError("Worksheet exceeds column limit")
        row = ET.SubElement(data, SHEET + "row", r=str(number))
        for index, value in enumerate(cells, 1):
            if value is None:
                continue
            address = column_name(index) + str(number)
            if isinstance(value, FormulaCell):
                cell = ET.SubElement(row, SHEET + "c", r=address)
                formula = value.formula.removeprefix("=")
                if not formula:
                    raise ValueError("Formula must not be empty")
                ET.SubElement(cell, SHEET + "f").text = checked_text(formula)
                continue
            if isinstance(value, (int, float)):
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("Numeric cells must be finite")
                cell = ET.SubElement(row, SHEET + "c", r=address,
                                     t="b" if isinstance(value, bool) else "n")
                ET.SubElement(cell, SHEET + "v").text = (
                    str(int(value)) if isinstance(value, bool) else str(value)
                )
                continue
            cell = ET.SubElement(
                row, SHEET + "c", r=address, t="inlineStr"
            )
            inline = ET.SubElement(cell, SHEET + "is")
            text = ET.SubElement(
                inline, SHEET + "t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}
            )
            text.text = checked_text(value)
    return package_document(
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        {
            "xl/workbook.xml": xml_bytes(workbook),
            "xl/_rels/workbook.xml.rels": xml_bytes(relationships),
            "xl/worksheets/sheet1.xml": xml_bytes(worksheet),
        },
    )
