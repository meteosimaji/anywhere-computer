import io
import shutil
import subprocess
import uuid
import zipfile

import pytest

from anywhere_computer.document_writer import create_word, create_workbook, create_workbooks
from anywhere_computer.documents import read_document
from anywhere_computer.engine import Engine
from anywhere_computer.models import FormulaCell, ReadDocument, Request


def test_generated_office_packages_use_default_opc_namespaces():
    for content in (create_word("hello"), create_workbook([["hello"]])):
        with zipfile.ZipFile(io.BytesIO(content)) as package:
            types = package.read("[Content_Types].xml")
            relationships = package.read("_rels/.rels")
        assert (b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                b'content-types"') in types
        assert (b'<Relationships xmlns="http://schemas.openxmlformats.org/'
                b'package/2006/relationships"') in relationships


def test_generated_word_opens_in_libreoffice(tmp_path):
    office = shutil.which("soffice")
    if office is None:
        pytest.skip("LibreOffice is not installed")
    source = tmp_path / "document.docx"
    source.write_bytes(create_word("日本語 & <text>\nsecond"))
    output = tmp_path / "rendered"
    output.mkdir()
    profile = (tmp_path / "libreoffice-profile").as_uri()
    completed = subprocess.run(
        [office, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pdf",
         "--outdir", str(output), str(source)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    pdf = output / "document.pdf"
    assert pdf.stat().st_size > 0
    extract = shutil.which("pdftotext")
    if extract is not None:
        rendered = subprocess.run([extract, str(pdf), "-"], capture_output=True,
                                  text=True, timeout=30, check=True)
        assert "日本語 & <text>" in rendered.stdout
        assert "second" in rendered.stdout


def test_generated_word_and_workbook_roundtrip(tmp_path):
    word = tmp_path / "new.docx"
    word.write_bytes(create_word(" 日本語 & <text>\tvalue\nsecond"))
    entries = read_document(ReadDocument(path=str(word)))["entries"]
    assert entries[0]["text"] == " 日本語 & <text>\tvalue"
    assert entries[1]["text"] == "second"
    book = tmp_path / "new.xlsx"
    book.write_bytes(create_workbook([["a", "=1+2"], ["日本語", " & "]], "Data"))
    cells = read_document(ReadDocument(path=str(book), cell_range="B1:B2"))["entries"]
    assert [cell["value"] for cell in cells] == ["=1+2", " & "]
    assert all(cell["formula"] is None for cell in cells)


def test_writer_rejects_invalid_xml_and_sheet_names():
    with pytest.raises(ValueError):
        create_word("invalid\x00")
    with pytest.raises(ValueError):
        create_workbook([], "bad/name")


@pytest.mark.parametrize("invalid", ["\ud800", "\ufffe", "\uffff", "\x01"])
def test_invalid_xml_characters_are_rejected(invalid):
    with pytest.raises(ValueError):
        create_word(invalid)
    with pytest.raises(ValueError):
        create_workbook([[invalid]])


def test_workbook_cells_preserve_explicit_types(tmp_path):
    path = tmp_path / "typed.xlsx"
    path.write_bytes(
        create_workbook([[12, 1.25, True, None, "=A1+1", FormulaCell(formula="=A1+1")]])
    )
    entries = read_document(ReadDocument(path=str(path)))["entries"]
    assert [cell["cell"] for cell in entries] == ["A1", "B1", "C1", "E1", "F1"]
    assert [cell["type"] for cell in entries] == ["n", "n", "b", "inlineStr", "n"]
    assert [cell["value"] for cell in entries[:3]] == ["12", "1.25", "1"]
    assert entries[3]["formula"] is None and entries[3]["value"] == "=A1+1"
    assert entries[4]["formula"] == "A1+1" and entries[4]["value"] is None
    with pytest.raises(ValueError, match="finite"):
        create_workbook([[float("nan")]])


def test_multiple_sheets_preserve_order_and_independent_cells(tmp_path):
    path = tmp_path / "multiple.xlsx"
    path.write_bytes(create_workbooks({"Second": [[1]], "First": [["different"]]}))
    result = read_document(ReadDocument(path=str(path), section="First"))
    assert [section["name"] for section in result["sections"]] == ["Second", "First"]
    assert result["entries"][0]["value"] == "different"
    assert read_document(ReadDocument(path=str(path)))["entries"][0]["value"] == "1"
    with pytest.raises(ValueError, match="unique"):
        create_workbooks({"Data": [], "data": []})


async def test_public_document_write_conflict_backup_and_restore(tmp_path):
    engine = Engine(tmp_path)
    path = tmp_path / "document.docx"

    async def call(tool, **arguments):
        return await engine.execute(
            Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)
        )

    try:
        first = await call("documents_write", path=str(path), format="docx", text="original")
        assert first.state == "completed"
        original = path.read_bytes()
        conflict = await call(
            "documents_write",
            path=str(path),
            format="docx",
            text="lost",
            mode="replace",
            expected_sha256="0" * 64,
        )
        assert conflict.state == "failed" and path.read_bytes() == original
        changed = await call(
            "documents_write",
            path=str(path),
            format="docx",
            text="new",
            mode="replace",
            expected_sha256=first.data["sha256"],
        )
        assert changed.state == "completed"
        restored = await call(
            "files_restore",
            path=str(path),
            backup_id=first.data["sha256"],
            expected_sha256=changed.data["sha256"],
        )
        assert restored.state == "completed" and path.read_bytes() == original
    finally:
        await engine.close()


async def test_public_workbook_write_keeps_cell_types(tmp_path):
    engine = Engine(tmp_path)
    path = tmp_path / "types.xlsx"
    try:
        reply = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_write", arguments={"path": str(path), "format": "xlsx",
                "rows": [[3, False, None, "=A1", {"formula": "A1+1"}]]}))
        assert reply.state == "completed"
        cells = read_document(ReadDocument(path=str(path)))["entries"]
        assert [cell["type"] for cell in cells] == ["n", "b", "inlineStr", "n"]
        assert cells[-1]["formula"] == "A1+1"
    finally:
        await engine.close()


async def test_public_multiple_sheets_and_range_read(tmp_path):
    engine = Engine(tmp_path)
    path = tmp_path / "multi.xlsx"
    try:
        written = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_write", arguments={"path": str(path), "format": "xlsx",
                "sheets": {"Data": [[1, 2]], "Summary": [[{"formula": "SUM(Data!A1:B1)"}]]}}))
        assert written.state == "completed"
        read = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_read", arguments={"path": str(path), "section": "Summary",
                                               "cell_range": "A1"}))
        assert read.state == "completed"
        assert read.data["entries"][0]["formula"] == "SUM(Data!A1:B1)"
        before = path.read_bytes()
        invalid = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_write", arguments={"path": str(path), "format": "xlsx",
                "rows": [], "sheets": {"A": []}, "mode": "replace",
                "expected_sha256": written.data["sha256"]}))
        assert invalid.state == "failed" and path.read_bytes() == before
    finally:
        await engine.close()
