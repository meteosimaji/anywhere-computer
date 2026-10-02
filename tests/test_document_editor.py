import io
import time
import uuid
import zipfile
from xml.etree import ElementTree as ET

import pytest

from anywhere_computer import document_editor
from anywhere_computer import files as file_module
from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.delegated_tasks import DelegatedTaskGrant, DelegatedTaskStore
from anywhere_computer.document_editor import edit_spreadsheet_cell
from anywhere_computer.document_writer import create_workbooks
from anywhere_computer.documents import SHEET, read_document
from anywhere_computer.engine import Engine
from anywhere_computer.files import Files, sha256
from anywhere_computer.models import EditSpreadsheetCell, FormulaCell, ReadDocument, Request


async def test_cell_preview_edit_preserves_other_parts_and_cells(tmp_path):
    path = tmp_path / "book.xlsx"
    content = create_workbooks({"Data": [["old", "keep"], [42, FormulaCell(formula="A1")]],
                                "Summary": [["untouched"]]})
    source = io.BytesIO(content)
    output = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as changed:
        changed.comment = b"workbook archive comment"
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
        assert not list(engine.files.backups.iterdir())

        applied = await call(**arguments)
        assert applied.state == "completed"
        assert applied.data["sha256"] == preview.data["sha256"]
        assert applied.data["backup_id"] == sha256(before)
        with zipfile.ZipFile(io.BytesIO(before)) as old, zipfile.ZipFile(path) as new:
            assert old.namelist() == new.namelist()
            assert old.comment == new.comment
            for name in old.namelist():
                if name != "xl/worksheets/sheet1.xml":
                    assert new.read(name) == old.read(name)
            old_tree = ET.fromstring(old.read("xl/worksheets/sheet1.xml"))
            new_tree = ET.fromstring(new.read("xl/worksheets/sheet1.xml"))
            old_cells = old_tree.findall(f"{SHEET}sheetData/{SHEET}row/{SHEET}c")
            new_cells = new_tree.findall(f"{SHEET}sheetData/{SHEET}row/{SHEET}c")
            assert len(old_cells) == len(new_cells) == 4
            for old_cell, new_cell in zip(old_cells[1:], new_cells[1:], strict=True):
                assert ET.tostring(old_cell) == ET.tostring(new_cell)
        data = read_document(ReadDocument(path=str(path), section="Data"))
        summary = read_document(ReadDocument(path=str(path), section="Summary"))
        assert data["entries"][0]["value"] == arguments["new_text"]
        assert summary["entries"][0]["value"] == "untouched"

        stale = await call(**arguments)
        assert stale.state == "failed"
        assert stale.data["error_code"] == "document_changed"
        assert stale.data["edit_applied"] is False
        assert path.read_bytes() != before
        wrong_old = await call(**{**arguments, "expected_sha256": applied.data["sha256"]})
        assert wrong_old.state == "failed"
        assert wrong_old.data["error_code"] == "cell_changed"
        assert wrong_old.data["edit_applied"] is False
        restored = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="files_restore", arguments={"path": str(path), "backup_id": sha256(before),
                                              "expected_sha256": applied.data["sha256"]}))
        assert restored.state == "completed" and path.read_bytes() == before
    finally:
        await engine.close()


async def test_cell_edit_rejects_nonstring_and_missing_cell(tmp_path):
    path = tmp_path / "book.xlsx"
    path.write_bytes(create_workbooks({"Data": [[FormulaCell(formula="1+1"), "old", 42, True]]}))
    before = path.read_bytes()
    engine = Engine(tmp_path / "state")
    try:
        for cell in ("A1", "C1", "D1", "E1"):
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


def _replace_parts(content, updates):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as original, zipfile.ZipFile(output, "w") as changed:
        for item in original.infolist():
            changed.writestr(item, updates.get(item.filename, original.read(item)))
        for name, data in updates.items():
            if name not in original.namelist():
                changed.writestr(name, data)
    return output.getvalue()


def _arguments(path, content, **updates):
    return EditSpreadsheetCell(path=str(path), sheet="Data", cell="A1", old_text="old",
                               new_text="new", expected_sha256=sha256(content), **updates)


@pytest.mark.parametrize("unsafe", ["signed", "signature_type", "macro_part", "macro_type",
                                    "mc_attribute", "mc_element", "rich_text", "duplicate_cell",
                                    "external_sheet", "aliased_sheet"])
def test_cell_edit_rejects_unpreservable_or_ambiguous_package(tmp_path, unsafe):
    path = tmp_path / "book.xlsx"
    content = create_workbooks({"Data": [["old", "keep"]], "Summary": [["untouched"]]})
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        types = ET.fromstring(archive.read("[Content_Types].xml"))
        relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    updates = {}
    if unsafe == "signed":
        updates["_xmlsignatures/sig1.xml"] = b"opaque signature"
    elif unsafe == "signature_type":
        types[1].set("ContentType",
                     "application/vnd.openxmlformats-package.digital-signature-xmlsignature+xml")
        updates["[Content_Types].xml"] = ET.tostring(types)
    elif unsafe == "macro_part":
        updates["xl/vbaProject.bin"] = b"opaque macro"
    elif unsafe == "macro_type":
        types[1].set("ContentType", "application/vnd.ms-excel.sheet.macroEnabled.main+xml")
        updates["[Content_Types].xml"] = ET.tostring(types)
    elif unsafe == "external_sheet":
        relations[0].set("TargetMode", "External")
        relations[0].set("Target", "https://must-not-be-fetched.invalid/worksheet.xml")
        updates["xl/_rels/workbook.xml.rels"] = ET.tostring(relations)
    elif unsafe == "aliased_sheet":
        relations[1].set("Target", relations[0].get("Target"))
        updates["xl/_rels/workbook.xml.rels"] = ET.tostring(relations)
    else:
        compatibility = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
        if unsafe == "mc_attribute":
            sheet.set(compatibility + "Ignorable", "x14ac")
        elif unsafe == "mc_element":
            ET.SubElement(sheet, compatibility + "AlternateContent")
        else:
            first = sheet.find(f"{SHEET}sheetData/{SHEET}row/{SHEET}c")
            assert first is not None
            if unsafe == "rich_text":
                inline = first[0]
                inline.remove(inline[0])
                ET.SubElement(ET.SubElement(inline, SHEET + "r"), SHEET + "t").text = "old"
            else:
                row = sheet.find(f"{SHEET}sheetData/{SHEET}row")
                assert row is not None
                row.append(ET.fromstring(ET.tostring(first)))
        updates["xl/worksheets/sheet1.xml"] = ET.tostring(sheet)
    content = _replace_parts(content, updates)
    path.write_bytes(content)
    (tmp_path / "state").mkdir()
    files = Files(tmp_path / "state")
    for preview in (True, False):
        with pytest.raises(ValueError):
            edit_spreadsheet_cell(files, _arguments(path, content, preview=preview))
        assert path.read_bytes() == content
        assert not list(files.backups.iterdir())


@pytest.mark.parametrize("stage", ["lock", "replace"])
async def test_cell_edit_rejects_late_change_without_overwriting_external_file(
    tmp_path, monkeypatch, stage,
):
    path = tmp_path / "book.xlsx"
    original = create_workbooks({"Data": [["old", "keep"]]})
    external = create_workbooks({"Data": [["external", "keep"]]})
    path.write_bytes(original)
    engine = Engine(tmp_path / "state")
    if stage == "lock":
        write = engine.files._write_bytes

        def changed_before_lock(*args):
            path.write_bytes(external)
            return write(*args)

        monkeypatch.setattr(engine.files, "_write_bytes", changed_before_lock)
    else:
        read = file_module.read_bytes
        target_reads = 0

        def changed_before_replace(target):
            nonlocal target_reads
            if target == path:
                target_reads += 1
                if target_reads == 2:
                    path.write_bytes(external)
            return read(target)

        monkeypatch.setattr(file_module, "read_bytes", changed_before_replace)
    try:
        reply = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_edit_cell", arguments=_arguments(path, original).model_dump()))
        assert reply.state == "failed" and reply.data["edit_applied"] is False
        assert reply.data["error_code"] == "document_changed"
        assert path.read_bytes() == external
        backup = engine.files.backups / sha256(original)
        assert backup.exists() is (stage == "replace")
        if backup.exists():
            assert backup.read_bytes() == original
    finally:
        await engine.close()


def test_cell_edit_enforces_input_and_output_size_limits(tmp_path, monkeypatch):
    path = tmp_path / "book.xlsx"
    original = create_workbooks({"Data": [["old"]]})
    path.write_bytes(original)
    (tmp_path / "state").mkdir()
    files = Files(tmp_path / "state")
    monkeypatch.setattr(file_module, "MAX_READ_BYTES", len(original) - 1)
    with pytest.raises(ValueError, match="16 MiB read limit"):
        edit_spreadsheet_cell(files, _arguments(path, original))
    monkeypatch.undo()
    monkeypatch.setattr(document_editor, "MAX_READ_BYTES", 1)
    with pytest.raises(ValueError, match="16 MiB file limit"):
        edit_spreadsheet_cell(files, _arguments(path, original))
    assert path.read_bytes() == original
    assert not list(files.backups.iterdir())


async def test_cell_edit_requires_own_grant_and_never_inherits_selected_file_child(tmp_path):
    path = tmp_path / "book.xlsx"
    sibling = tmp_path / "private.xlsx"
    original = create_workbooks({"Data": [["old", "keep"]]})
    path.write_bytes(original)
    sibling.write_bytes(original)
    engine = Engine(tmp_path / "engine")
    authority = AuthorizationStore(tmp_path / "authority", resource="https://fixture.example/mcp",
                                   known_tools=frozenset(engine.tools))
    authority.register_client("client", frozenset({"https://client.example/callback"}))
    granted = frozenset({"documents_write", "documents_edit_cell", "files_read", "files_write",
                         "operations_get"})
    authority.enroll_device("owner", "device", granted)
    authority.enroll_device("owner", "other-device", granted)

    def issue(device, tools):
        code = authority.approve(owner="owner", device=device, client="client",
            redirect="https://client.example/callback", resource=authority.resource,
            tools=frozenset(tools), challenge=pkce_s256("v" * 43))
        return authority.exchange_code(code=code, verifier="v" * 43, client="client",
            redirect="https://client.example/callback", resource=authority.resource).value

    delegation = DelegatedTaskStore(tmp_path / "delegation", authority)
    backend = AuthorizedDeviceMCP(authority, engine, owner="owner", device="device",
                                  delegated_tasks=delegation)
    try:
        old_id = await backend.authenticate(issue("device", {"documents_write"}))
        assert old_id is not None
        old_session = backend.session(old_id)
        assert {tool["name"] for tool in await old_session.catalog()} == {"documents_write"}
        denied = await old_session.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_edit_cell", arguments=_arguments(path, original).model_dump()))
        assert denied.state == "failed" and denied.data["dispatched"] is False
        assert denied.data["error_code"] == "capability_not_authorized"
        assert await backend.authenticate(issue("other-device", granted)) is None
        parent_id = await backend.authenticate(issue("device", granted))
        assert parent_id is not None
        child_id = uuid.uuid4().hex
        delegation.issue(DelegatedTaskGrant(owner="owner", child_id=child_id,
            parent_grant_id=parent_id, device_id="local", tools=frozenset({"files_read"}),
            read_files=(str(path),), expires_at=time.time() + 600))
        child = backend.session("child:" + child_id)
        assert "documents_edit_cell" not in {tool["name"] for tool in await child.catalog()}
        for target in (path, sibling):
            refused = await child.execute(Request(operation_id=uuid.uuid4().hex,
                tool="documents_edit_cell", arguments=_arguments(target, original).model_dump()))
            assert refused.state == "failed"
            assert refused.data == {"dispatched": False, "reason": "tool_out_of_scope"}
            assert target.read_bytes() == original
        # As with the existing document writer, a path-confined document editor
        # has not been implemented. A fixture owner cannot issue such a child grant.
        for tool in ("documents_write", "documents_edit_cell"):
            with pytest.raises(ValueError, match="cannot issue this delegation"):
                delegation.issue(DelegatedTaskGrant(owner="owner", child_id=uuid.uuid4().hex,
                    parent_grant_id=parent_id, device_id="local", tools=frozenset({tool}),
                    write_roots=(str(tmp_path),), expires_at=time.time() + 600))
        parent = backend.session(parent_id)
        preview = await parent.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_edit_cell",
            arguments=_arguments(path, original, preview=True).model_dump()))
        assert preview.state == "completed" and path.read_bytes() == original
        applied = await parent.execute(Request(operation_id=uuid.uuid4().hex,
            tool="documents_edit_cell", arguments=_arguments(path, original).model_dump()))
        assert applied.state == "completed" and applied.data["backup_id"] == sha256(original)
        assert sibling.read_bytes() == original
        authority.revoke(owner="owner", grant=parent_id)
        with pytest.raises(ValueError, match="no longer available"):
            await parent.catalog()
        assert path.read_bytes() != original
    finally:
        await backend.close()
        delegation.close()
        authority.close()
        await engine.close()
