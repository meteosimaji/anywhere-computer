import uuid

import pytest

from anywhere_computer.document_writer import create_word, create_workbook
from anywhere_computer.documents import read_document
from anywhere_computer.engine import Engine
from anywhere_computer.models import ReadDocument, Request


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


async def test_public_document_write_conflict_backup_and_restore(tmp_path):
    engine = Engine(tmp_path)
    path = tmp_path / "document.docx"

    async def call(tool, **arguments):
        return await engine.execute(Request(operation_id=uuid.uuid4().hex,
                                            tool=tool, arguments=arguments))

    try:
        first = await call("documents_write", path=str(path), format="docx", text="original")
        assert first.state == "completed"
        original = path.read_bytes()
        conflict = await call("documents_write", path=str(path), format="docx",
                              text="lost", mode="replace", expected_sha256="0" * 64)
        assert conflict.state == "failed" and path.read_bytes() == original
        changed = await call("documents_write", path=str(path), format="docx", text="new",
                             mode="replace", expected_sha256=first.data["sha256"])
        assert changed.state == "completed"
        restored = await call("files_restore", path=str(path), backup_id=first.data["sha256"],
                              expected_sha256=changed.data["sha256"])
        assert restored.state == "completed" and path.read_bytes() == original
    finally:
        await engine.close()
