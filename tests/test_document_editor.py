import io
import uuid
import zipfile
from xml.etree import ElementTree as ET

from anywhere_computer.document_writer import create_workbooks
from anywhere_computer.documents import SHEET, read_document
from anywhere_computer.engine import Engine
from anywhere_computer.files import sha256
from anywhere_computer.models import FormulaCell, ReadDocument, Request


async def test_cell_preview_edit_preserves_other_parts_and_cells(tmp_path):
    path = tmp_path / "book.xlsx"
    content = create_workbooks({"Data": [["old", "keep"]], "Summary": [["untouched"]]})
    source = io.BytesIO(content)
    output = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
        for item in original.infolist():
            changed.writestr(item, original.read(item))
        changed.writestr("custom/opaque.bin", b"preserve exactly")
    path.write_bytes(output.getvalue())
    before = path.read_bytes()
    engine = Engine(tmp_path / "state")

    async def call(**arguments):
        return await engine.execute(Request(operation_id=uuid.uuid4().hex,
                                            tool="documents_edit_cell", arguments=arguments))

    arguments = {"path": str(path), "sheet": "Data", "cell": "A1", "old_text": "old",
                 "new_text": " 日本語 & <changed> ", "expected_sha256": sha256(before)}
    try:
        preview = await call(**arguments, preview=True)
        assert preview.state == "completed"
        assert preview.data["diff"] == {"old": "old", "new": " 日本語 & <changed> "}
        assert path.read_bytes() == before

        applied = await call(**arguments)
        assert applied.state == "completed"
        assert applied.data["sha256"] == preview.data["sha256"]
        assert applied.data["backup_id"] == sha256(before)
        with zipfile.ZipFile(io.BytesIO(before)) as old, zipfile.ZipFile(path) as new:
            assert old.namelist() == new.namelist()
            for name in old.namelist():
                if name != "xl/worksheets/sheet1.xml":
                    assert new.read(name) == old.read(name)
            old_tree = ET.fromstring(old.read("xl/worksheets/sheet1.xml"))
            new_tree = ET.fromstring(new.read("xl/worksheets/sheet1.xml"))
            old_cells = old_tree.findall(f"{SHEET}sheetData/{SHEET}row/{SHEET}c")
            new_cells = new_tree.findall(f"{SHEET}sheetData/{SHEET}row/{SHEET}c")
            assert ET.tostring(old_cells[1]) == ET.tostring(new_cells[1])
        data = read_document(ReadDocument(path=str(path), section="Data"))
        summary = read_document(ReadDocument(path=str(path), section="Summary"))
        assert data["entries"][0]["value"] == arguments["new_text"]
        assert summary["entries"][0]["value"] == "untouched"

        stale = await call(**arguments)
        assert stale.state == "failed"
        assert path.read_bytes() != before
        wrong_old = await call(**{**arguments, "expected_sha256": applied.data["sha256"]})
        assert wrong_old.state == "failed"
        restored = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="files_restore", arguments={"path": str(path), "backup_id": sha256(before),
                                              "expected_sha256": applied.data["sha256"]}))
        assert restored.state == "completed" and path.read_bytes() == before
    finally:
        await engine.close()


async def test_cell_edit_rejects_formula_and_missing_cell(tmp_path):
    path = tmp_path / "book.xlsx"
    path.write_bytes(create_workbooks({"Data": [[FormulaCell(formula="1+1"), "old"]]}))
    before = path.read_bytes()
    engine = Engine(tmp_path / "state")
    try:
        for cell in ("A1", "C1"):
            result = await engine.execute(Request(operation_id=uuid.uuid4().hex,
                tool="documents_edit_cell", arguments={"path": str(path), "sheet": "Data",
                    "cell": cell, "old_text": "old", "new_text": "new",
                    "expected_sha256": sha256(before)}))
            assert result.state == "failed"
            assert path.read_bytes() == before
    finally:
        await engine.close()


async def test_edit_shared_string_keeps_table_and_cell_style(tmp_path):
    path = tmp_path / "shared.xlsx"
    source = create_workbooks({"Data": [["old", "keep"]]})
    with zipfile.ZipFile(io.BytesIO(source)) as original:
        members = {name: original.read(name) for name in original.namelist()}
    worksheet = ET.fromstring(members["xl/worksheets/sheet1.xml"])
    first = worksheet.find(f"{SHEET}sheetData/{SHEET}row/{SHEET}c")
    assert first is not None
    first.set("t", "s")
    first.set("s", "4")
    first.remove(first[0])
    ET.SubElement(first, SHEET + "v").text = "0"
    members["xl/worksheets/sheet1.xml"] = ET.tostring(worksheet)
    relations = ET.fromstring(members["xl/_rels/workbook.xml.rels"])
    namespace = "http://schemas.openxmlformats.org/package/2006/relationships"
    ET.SubElement(relations, f"{{{namespace}}}Relationship", Id="shared",
                  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings",
                  Target="sharedStrings.xml")
    members["xl/_rels/workbook.xml.rels"] = ET.tostring(relations)
    members["xl/sharedStrings.xml"] = (
        f'<sst xmlns="{SHEET[1:-1]}"><si><t>old</t></si></sst>'.encode()
    )
    content_types = ET.fromstring(members["[Content_Types].xml"])
    types_namespace = "http://schemas.openxmlformats.org/package/2006/content-types"
    ET.SubElement(content_types, f"{{{types_namespace}}}Override",
                  PartName="/xl/sharedStrings.xml",
                  ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml."
                              "sharedStrings+xml")
    members["[Content_Types].xml"] = ET.tostring(content_types)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    path.write_bytes(output.getvalue())
    engine = Engine(tmp_path / "state")
    try:
        result = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_edit_cell", arguments={"path": str(path), "sheet": "Data",
                "cell": "A1", "old_text": "old", "new_text": "new",
                "expected_sha256": sha256(path.read_bytes())}))
        assert result.state == "completed"
        with zipfile.ZipFile(path) as archive:
            assert archive.read("xl/sharedStrings.xml") == members["xl/sharedStrings.xml"]
            edited = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            cells = edited.findall(f"{SHEET}sheetData/{SHEET}row/{SHEET}c")
            assert cells[0].get("s") == "4" and cells[0].get("t") == "inlineStr"
            assert cells[1].find(f"{SHEET}is/{SHEET}t").text == "keep"
        assert read_document(ReadDocument(path=str(path)))["entries"][0]["value"] == "new"
    finally:
        await engine.close()
