"""Small OOXML document generation using only ZIP and XML from the standard library."""

import io
import zipfile
from typing import cast
from xml.etree import ElementTree as ET

from pydantic import JsonValue

from .documents import REL, SHEET, WORD
from .files import Files, absolute_path
from .models import WriteDocument

CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def write_document(files: Files, args: WriteDocument) -> dict[str, JsonValue]:
    if absolute_path(args.path).suffix.lower() != "." + args.format:
        raise ValueError("Document extension must match the requested format")
    if args.format == "docx":
        if args.text is None or args.rows is not None:
            raise ValueError("Word generation requires text and does not accept rows")
        content = create_word(args.text)
    else:
        if args.rows is None or args.text is not None:
            raise ValueError("Workbook generation requires rows and does not accept text")
        content = create_workbook(args.rows, args.sheet_name)
    return files._write_bytes(args.path, content, args.mode, args.expected_sha256)


def xml_bytes(root: ET.Element) -> bytes:
    return cast(bytes, ET.tostring(root, encoding="utf-8", xml_declaration=True))


def package_document(main: str, content_type: str, parts: dict[str, bytes]) -> bytes:
    types = ET.Element(f"{{{CONTENT_TYPES}}}Types")
    ET.SubElement(
        types,
        f"{{{CONTENT_TYPES}}}Default",
        Extension="rels",
        ContentType="application/vnd.openxmlformats-package.relationships+xml",
    )
    ET.SubElement(
        types, f"{{{CONTENT_TYPES}}}Default", Extension="xml", ContentType="application/xml"
    )
    ET.SubElement(
        types, f"{{{CONTENT_TYPES}}}Override", PartName="/" + main, ContentType=content_type
    )
    for name in parts:
        if name.startswith("xl/worksheets/"):
            ET.SubElement(
                types,
                f"{{{CONTENT_TYPES}}}Override",
                PartName="/" + name,
                ContentType="application/vnd.openxmlformats-officedocument."
                "spreadsheetml.worksheet+xml",
            )
    relationships = ET.Element(f"{{{REL}}}Relationships")
    ET.SubElement(
        relationships,
        f"{{{REL}}}Relationship",
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
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
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


def create_workbook(rows: list[list[str]], sheet_name: str = "Sheet1") -> bytes:
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
    if sum(len(cell) for row in rows for cell in row) > 1000000:
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
            cell = ET.SubElement(
                row, SHEET + "c", r=column_name(index) + str(number), t="inlineStr"
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
