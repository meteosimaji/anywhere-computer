"""Bounded OOXML text inspection, without Office automation or format libraries."""

import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from pydantic import JsonValue

from .files import absolute_path, read_bytes, sha256
from .models import ReadDocument

REL = "http://schemas.openxmlformats.org/package/2006/relationships"
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PRESENT = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
DRAW = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
XML_LIMIT = 4 * 1024 * 1024
EXTRACTION_XML_LIMIT = 64 * 1024 * 1024
EXTRACTION_TEXT_LIMIT = 16 * 1024 * 1024
EXTRACTION_NODE_LIMIT = 500000
EXTRACTION_ENTRY_LIMIT = 100000


@dataclass
class ExtractionBudget:
    """Charge repeated work, not just unique ZIP members, before accumulating it."""

    xml_bytes: int = 0
    text_bytes: int = 0
    nodes: int = 0
    entries: int = 0

    def xml(self, size: int) -> None:
        self.xml_bytes += size
        if self.xml_bytes > EXTRACTION_XML_LIMIT:
            raise ValueError("Document extraction XML limit exceeded")

    def visit(self) -> None:
        self.nodes += 1
        if self.nodes > EXTRACTION_NODE_LIMIT:
            raise ValueError("Document extraction traversal limit exceeded")

    def text(self, value: str) -> None:
        # Four bytes per Unicode code point bounds UTF-8 and Python's string storage,
        # without allocating a second encoded copy merely to measure hostile text.
        self.text_bytes += len(value) * 4
        if self.text_bytes > EXTRACTION_TEXT_LIMIT:
            raise ValueError("Document extraction text limit exceeded")

    def append(self, entries: list[JsonValue], entry: dict[str, JsonValue]) -> None:
        self.entries += 1
        if self.entries > EXTRACTION_ENTRY_LIMIT:
            raise ValueError("Document extraction entry limit exceeded")
        for value in entry.values():
            if isinstance(value, str):
                self.text(value)
        entries.append(entry)


def cell_position(reference: str) -> tuple[int, int]:
    match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})", reference)
    if match is None:
        raise ValueError("Invalid A1 cell reference")
    column = 0
    for letter in match[1].upper():
        column = column * 26 + ord(letter) - ord("A") + 1
    row = int(match[2])
    if column > 16384 or row > 1048576:
        raise ValueError("Cell reference exceeds supported worksheet bounds")
    return row, column


def cell_bounds(value: str) -> tuple[int, int, int, int]:
    parts = value.split(":")
    if len(parts) not in (1, 2):
        raise ValueError("Use one cell or a rectangular A1 range")
    first, last = cell_position(parts[0]), cell_position(parts[-1])
    if first[0] > last[0] or first[1] > last[1]:
        raise ValueError("Cell range endpoints are reversed")
    return first[0], first[1], last[0], last[1]


class OfficePackage:
    def __init__(self, content: bytes) -> None:
        self.budget = ExtractionBudget()
        self.archive = zipfile.ZipFile(io.BytesIO(content))
        items = self.archive.infolist()
        names = [item.filename for item in items]
        if len(items) > 4096 or len(names) != len(set(names)):
            self.archive.close()
            raise ValueError("Package has too many or duplicate entries")
        if sum(item.file_size for item in items) > 64 * 1024 * 1024:
            self.archive.close()
            raise ValueError("Package uncompressed size exceeds 64 MiB")

    def xml(self, part: str) -> ET.Element:
        info = self.archive.getinfo(part)
        if info.file_size > XML_LIMIT:
            raise ValueError("XML part exceeds 4 MiB")
        self.budget.xml(info.file_size)
        with self.archive.open(info) as stream:
            raw = stream.read(XML_LIMIT + 1)
        if len(raw) > XML_LIMIT:
            raise ValueError("XML part exceeds 4 MiB")
        # Restrict to the usual OOXML UTF-8 encoding so DTD filtering is unambiguous.
        text = raw.decode("utf-8-sig")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("XML entity and document type declarations are unsupported")
        root = ET.fromstring(text)
        for _ in root.iter():
            self.budget.visit()
        return root

    def relationships(self, part: str) -> dict[str, tuple[str, str]]:
        base, name = posixpath.split(part)
        relpath = posixpath.join(base, "_rels", name + ".rels") if part else "_rels/.rels"
        result: dict[str, tuple[str, str]] = {}
        for relation in self.xml(relpath).findall(f"{{{REL}}}Relationship"):
            if relation.get("TargetMode") == "External":
                continue
            target = unquote(relation.get("Target", ""))
            if not target or ":" in target or "\\" in target:
                raise ValueError("Invalid internal relationship")
            target = posixpath.normpath(
                target.lstrip("/") if target.startswith("/") else posixpath.join(base, target)
            )
            if target == ".." or target.startswith("../"):
                raise ValueError("Relationship leaves the package")
            identity = relation.get("Id", "")
            if not identity or identity in result:
                raise ValueError("Duplicate or missing relationship ID")
            result[identity] = (relation.get("Type", ""), target)
        return result

    def main_part(self) -> str:
        matches = [
            target
            for kind, target in self.relationships("").values()
            if kind.endswith("/officeDocument")
        ]
        if len(matches) != 1:
            raise ValueError("Package needs one main office document")
        return matches[0]


def text_runs(element: ET.Element, namespace: str, budget: ExtractionBudget) -> str:
    pieces = []
    for child in element.iter():
        budget.visit()
        if child.tag == namespace + "t":
            piece = child.text or ""
        elif child.tag == namespace + "tab":
            piece = "\t"
        elif child.tag in (namespace + "br", namespace + "cr"):
            piece = "\n"
        else:
            continue
        budget.text(piece)
        pieces.append(piece)
    return "".join(pieces)


def word_paragraphs(body: ET.Element, budget: ExtractionBudget) -> list[JsonValue]:
    """Keep document order and nearest table cell without recursively walking XML."""
    entries: list[JsonValue] = []
    pending: list[tuple[ET.Element, dict[str, JsonValue]]] = [(body, {})]
    table_count = 0
    while pending:
        element, location = pending.pop()
        budget.visit()
        if element.tag == WORD + "tbl":
            table_count += 1
            location = {"table": table_count}
        if element.tag == WORD + "p":
            budget.append(entries, {"paragraph": len(entries) + 1,
                                    "text": text_runs(element, WORD, budget), **location})
        children = []
        row_index = cell_index = 0
        for child in element:
            child_location = location
            if element.tag == WORD + "tbl" and child.tag == WORD + "tr":
                row_index += 1
                child_location = {**location, "row_index": row_index}
            elif element.tag == WORD + "tr" and child.tag == WORD + "tc":
                cell_index += 1
                child_location = {**location, "cell_index": cell_index}
            children.append((child, child_location))
        pending.extend(reversed(children))
    return entries


def read_document(args: ReadDocument) -> dict[str, JsonValue]:
    path = absolute_path(args.path)
    content = read_bytes(path)
    package = OfficePackage(content)
    budget = package.budget
    try:
        part = package.main_part()
        root = package.xml(part)
        bounds = cell_bounds(args.cell_range) if args.cell_range is not None else None
        if bounds is not None and root.tag != SHEET + "workbook":
            raise ValueError("Cell ranges apply only to spreadsheets")
        entries: list[JsonValue] = []
        sections: list[JsonValue] = []
        kind: str
        if root.tag == WORD + "document":
            kind = "docx"
            if args.section is not None:
                raise ValueError("Word main-body reading does not use section selection")
            body = root.find(WORD + "body")
            if body is None:
                raise ValueError("Word document has no body")
            entries = word_paragraphs(body, budget)
        elif root.tag == SHEET + "workbook":
            kind = "xlsx"
            relations = package.relationships(part)
            sheets = root.find(SHEET + "sheets")
            if sheets is None:
                raise ValueError("Workbook has no sheets")
            shared = []
            for relation_kind, target in relations.values():
                if relation_kind.endswith("/sharedStrings"):
                    shared = [
                        text_runs(item, SHEET, budget)
                        for item in package.xml(target).findall(SHEET + "si")
                    ]
            selected = None
            for sheet in sheets:
                name = sheet.get("name", "")
                budget.append(sections, {"name": name, "state": sheet.get("state", "visible")})
                if (args.section is None and selected is None) or args.section == name:
                    selected = sheet
            if selected is None:
                raise ValueError("Worksheet was not found")
            relation_kind, target = relations[selected.get(RID, "")]
            if not relation_kind.endswith("/worksheet"):
                raise ValueError("Selected sheet is not a worksheet")
            data = package.xml(target).find(SHEET + "sheetData")
            if data is not None:
                for row in data:
                    for cell in row.findall(SHEET + "c"):
                        if bounds is not None:
                            cell_row, cell_column = cell_position(cell.get("r", ""))
                            if not (bounds[0] <= cell_row <= bounds[2]
                                    and bounds[1] <= cell_column <= bounds[3]):
                                continue
                        value = cell.findtext(SHEET + "v")
                        cell_type = cell.get("t", "n")
                        if cell_type == "s":
                            index = int(value or "-1")
                            if index < 0 or index >= len(shared):
                                raise ValueError("Invalid shared string reference")
                            value = shared[index]
                        elif cell_type == "inlineStr":
                            value = text_runs(cell, SHEET, budget)
                        budget.append(entries,
                            {
                                "sheet": selected.get("name", ""),
                                "cell": cell.get("r", ""),
                                "type": cell_type,
                                "value": value,
                                "formula": cell.findtext(SHEET + "f"),
                                "style_index": cell.get("s"),
                            }
                        )
        elif root.tag == PRESENT + "presentation":
            kind = "pptx"
            relations = package.relationships(part)
            slides = root.find(PRESENT + "sldIdLst")
            if slides is not None:
                for index, slide in enumerate(slides):
                    number = str(index + 1)
                    budget.append(sections, {"name": number})
                    if args.section is not None and args.section != number:
                        continue
                    relation_kind, target = relations[slide.get(RID, "")]
                    if not relation_kind.endswith("/slide"):
                        raise ValueError("Presentation references a non-slide part")
                    for paragraph in package.xml(target).iter(DRAW + "p"):
                        budget.append(entries, {"slide": index + 1,
                                                "text": text_runs(paragraph, DRAW, budget)})
            if args.section is not None and not any(
                item == {"name": args.section} for item in sections
            ):
                raise ValueError("Slide was not found")
        else:
            raise ValueError("Unsupported OOXML document namespace or kind")
        page: list[JsonValue] = []
        page_bytes = 0
        for entry in entries[args.offset : args.offset + args.limit]:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            for key, field_value in list(item.items()):
                if isinstance(field_value, str) and len(field_value) > 32768:
                    item[key] = field_value[:32768]
                    item["text_truncated"] = True
            size = len(json.dumps(item, ensure_ascii=False).encode())
            if page and page_bytes + size > 512000:
                break
            page.append(item)
            page_bytes += size
        return {
            "path": str(path),
            "sha256": sha256(content),
            "format": kind,
            "sections": sections,
            "entries": page,
            "total_entries": len(entries),
            "next_offset": args.offset + len(page),
            "truncated": args.offset + len(page) < len(entries),
            "rendered": False,
            "formulas_evaluated": False,
        }
    except (KeyError, zipfile.BadZipFile, ET.ParseError) as error:
        raise ValueError("Office package is malformed or references an unavailable part") from error
    finally:
        package.archive.close()
