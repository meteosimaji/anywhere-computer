import io
import shutil
import subprocess
import uuid
import zipfile

import pytest

from anywhere_computer.document_writer import (
    create_word,
    create_workbook,
    create_workbooks,
    edit_document_paragraph,
)
from anywhere_computer.documents import WORD, read_document
from anywhere_computer.engine import Engine
from anywhere_computer.files import Files, sha256
from anywhere_computer.models import EditDocumentParagraph, FormulaCell, ReadDocument, Request


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


async def test_targeted_docx_edit_reports_diff_and_preserves_other_parts(tmp_path):
    engine = Engine(tmp_path / "state")
    path = tmp_path / "paragraphs.docx"
    path.write_bytes(create_word("first\n日本語 42\nlast"))
    original = path.read_bytes()
    digest = sha256(original)
    try:
        conflict = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_edit_paragraph", arguments={"path": str(path), "paragraph": 2,
                "expected_sha256": "0" * 64, "expected_text": "日本語 42",
                "new_text": "日本語 43"}))
        assert conflict.state == "failed" and path.read_bytes() == original
        edited = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_edit_paragraph", arguments={"path": str(path), "paragraph": 2,
                "expected_sha256": digest, "expected_text": "日本語 42",
                "new_text": "日本語 43"}))
        assert edited.state == "completed", edited.error
        assert edited.data["diff"] == {
            "paragraph": 2, "before": "日本語 42", "after": "日本語 43",
        }
        assert edited.data["backup_id"] == digest
        entries = read_document(ReadDocument(path=str(path)))["entries"]
        assert [entry["text"] for entry in entries] == [
            "first", "日本語 43", "last",
        ]
        with zipfile.ZipFile(io.BytesIO(original)) as before, zipfile.ZipFile(path) as after:
            assert before.namelist() == after.namelist()
            for name in before.namelist():
                if name != "word/document.xml":
                    assert before.read(name) == after.read(name)
        restored = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="files_restore", arguments={"path": str(path), "backup_id": digest,
                "expected_sha256": edited.data["sha256"]}))
        assert restored.state == "completed" and path.read_bytes() == original
    finally:
        await engine.close()


def test_targeted_docx_edit_rejects_mismatch_and_rich_paragraph(tmp_path):
    path = tmp_path / "paragraphs.docx"
    path.write_bytes(create_word("first\nsecond"))
    original = path.read_bytes()
    (tmp_path / "state").mkdir()
    files = Files(tmp_path / "state")
    base = {"path": str(path), "paragraph": 2, "expected_sha256": sha256(original)}
    with pytest.raises(ValueError, match="Paragraph changed"):
        edit_document_paragraph(files, EditDocumentParagraph(**base, expected_text="wrong",
                             new_text="changed"))
    with pytest.raises(ValueError, match="single plain-text paragraph"):
        edit_document_paragraph(files, EditDocumentParagraph(**base, expected_text="second",
                             new_text="with\nline break"))
    assert path.read_bytes() == original

    rich = tmp_path / "rich.docx"
    with zipfile.ZipFile(io.BytesIO(create_word("second"))) as source:
        with zipfile.ZipFile(rich, "w") as destination:
            for info in source.infolist():
                payload = source.read(info)
                if info.filename == "word/document.xml":
                    payload = (
                        f'<w:document xmlns:w="{WORD[1:-1]}"><w:body><w:p>'
                        '<w:r><w:t>sec</w:t></w:r><w:r><w:t>ond</w:t></w:r>'
                        '</w:p></w:body></w:document>'
                    ).encode()
                destination.writestr(info, payload)
    rich_original = rich.read_bytes()
    with pytest.raises(ValueError, match="single plain-text run"):
        edit_document_paragraph(files, EditDocumentParagraph(
            path=str(rich), paragraph=1, expected_sha256=sha256(rich_original),
            expected_text="second", new_text="changed",
        ))
    assert rich.read_bytes() == rich_original


def test_targeted_docx_edit_selects_nested_text_box_paragraph(tmp_path):
    path = tmp_path / "textbox.docx"
    vml = "urn:schemas-microsoft-com:vml"
    document = (
        f'<w:document xmlns:w="{WORD[1:-1]}" xmlns:v="{vml}"><w:body>'
        '<w:p><w:r><w:t>Outer</w:t></w:r><w:r><w:pict><v:shape>'
        '<v:textbox><w:txbxContent><w:p><w:r><w:t>Inner</w:t></w:r></w:p>'
        '</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>'
        '</w:body></w:document>'
    ).encode()
    with zipfile.ZipFile(io.BytesIO(create_word("placeholder"))) as source:
        with zipfile.ZipFile(path, "w") as destination:
            for info in source.infolist():
                destination.writestr(info, document if info.filename == "word/document.xml"
                                     else source.read(info))
    original = path.read_bytes()
    (tmp_path / "state").mkdir()
    files = Files(tmp_path / "state")
    result = edit_document_paragraph(files, EditDocumentParagraph(
        path=str(path), paragraph=2, expected_sha256=sha256(original),
        expected_text="Inner", new_text="Changed",
    ))
    assert result["diff"] == {"paragraph": 2, "before": "Inner", "after": "Changed"}
    assert [entry["text"] for entry in read_document(ReadDocument(path=str(path)))["entries"]] == [
        "Outer", "Changed",
    ]
    with zipfile.ZipFile(io.BytesIO(original)) as before, zipfile.ZipFile(path) as after:
        for name in before.namelist():
            if name != "word/document.xml":
                assert after.read(name) == before.read(name)


def test_targeted_docx_edit_refuses_signed_package(tmp_path):
    path = tmp_path / "signed.docx"
    with zipfile.ZipFile(io.BytesIO(create_word("original"))) as source:
        with zipfile.ZipFile(path, "w") as destination:
            for info in source.infolist():
                destination.writestr(info, source.read(info))
            destination.writestr("_xmlsignatures/sig1.xml", b"<Signature/>")
    original = path.read_bytes()
    (tmp_path / "state").mkdir()
    files = Files(tmp_path / "state")
    with pytest.raises(ValueError, match="Signed documents"):
        edit_document_paragraph(files, EditDocumentParagraph(
            path=str(path), paragraph=1, expected_sha256=sha256(original),
            expected_text="original", new_text="changed",
        ))
    assert path.read_bytes() == original


@pytest.mark.parametrize("compatibility", [
    'mc:Ignorable="w14"',
    '<mc:AlternateContent/>',
])
def test_targeted_docx_edit_refuses_markup_compatibility_without_changing_file(
    tmp_path, compatibility,
):
    path = tmp_path / "compatible.docx"
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    if compatibility.startswith("mc:Ignorable"):
        opening = f'<w:document xmlns:w="{WORD[1:-1]}" xmlns:mc="{mc}" '
        opening += f'xmlns:w14="urn:word-2010" {compatibility}>'
        inner = ""
    else:
        opening = f'<w:document xmlns:w="{WORD[1:-1]}" xmlns:mc="{mc}">'
        inner = compatibility
    xml = (opening + '<w:body><w:p><w:r><w:t>original</w:t></w:r></w:p>'
           + inner + '</w:body></w:document>').encode()
    with zipfile.ZipFile(io.BytesIO(create_word("original"))) as source:
        with zipfile.ZipFile(path, "w") as destination:
            for info in source.infolist():
                destination.writestr(
                    info, xml if info.filename == "word/document.xml" else source.read(info)
                )
    original = path.read_bytes()
    (tmp_path / "state").mkdir()
    files = Files(tmp_path / "state")
    with pytest.raises(ValueError, match="Markup compatibility"):
        edit_document_paragraph(files, EditDocumentParagraph(
            path=str(path), paragraph=1, expected_sha256=sha256(original),
            expected_text="original", new_text="changed",
        ))
    assert path.read_bytes() == original
