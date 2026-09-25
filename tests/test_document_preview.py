import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile

import psutil
import pytest

from anywhere_computer.document_preview import _run, _sandbox_policy, preview_document
from anywhere_computer.engine import Engine
from anywhere_computer.files import sha256
from anywhere_computer.mcp_server import MCPSession
from anywhere_computer.models import PreviewDocument, Request

REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(path, *, external=False):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/'
                         'package/2006/content-types"><Default Extension="rels" '
                         'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/word/document.xml" ContentType="application/'
                         'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                         '</Types>')
        archive.writestr("_rels/.rels", f'<Relationships xmlns="{REL}">'
                         f'<Relationship Id="main" Type="{OFFICE_REL}/officeDocument" '
                         'Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", f'<w:document xmlns:w="{WORD}"><w:body>'
                         '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/>'
                         '<w:sz w:val="36"/></w:rPr><w:t>Formatted heading</w:t></w:r></w:p>'
                         '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
                         '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Second page table</w:t>'
                         '</w:r></w:p></w:tc></w:tr></w:tbl>'
                         '</w:body></w:document>')
        if external:
            archive.writestr("word/_rels/document.xml.rels", f'<Relationships xmlns="{REL}">'
                             '<Relationship Id="external" TargetMode="External" '
                             'Type="hyperlink" Target="https://example.invalid/"/>'
                             '</Relationships>')
    return path


def args(path, page=1):
    return PreviewDocument(path=str(path), expected_sha256=sha256(path.read_bytes()), page=page)


def test_formatted_multipage_docx_renders_distinct_pages_without_changing_source(tmp_path):
    if any(shutil.which(name) is None for name in
           ("soffice", "pdfinfo", "pdftoppm", "sandbox-exec")):
        pytest.skip("Local sandboxed document renderer is unavailable")
    path = docx(tmp_path / "formatted.docx")
    before = path.read_bytes()
    first = preview_document(args(path))
    second = preview_document(args(path, 2))
    assert first["pages"] == second["pages"] == 2
    assert first["rendered"] is second["rendered"] is True
    assert first["mime_type"] == second["mime_type"] == "image/png"
    png_one = base64.b64decode(first["data_base64"])
    png_two = base64.b64decode(second["data_base64"])
    assert png_one.startswith(b"\x89PNG\r\n\x1a\n") and png_two != png_one
    assert path.read_bytes() == before


async def test_rendered_preview_reaches_mcp_result_with_bounded_image(tmp_path):
    if sys.platform != "darwin" or any(shutil.which(name) is None for name in
           ("soffice", "pdfinfo", "pdftoppm", "sandbox-exec")):
        pytest.skip("Local sandboxed document renderer is unavailable")
    path = docx(tmp_path / "formatted.docx")
    engine = Engine(tmp_path / "engine")
    try:
        async def catalog():
            return engine.catalog()

        session = MCPSession(catalog, engine.execute)
        initialized = await session.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "preview-test", "version": "1"}},
        })
        assert initialized["result"]["serverInfo"]["name"] == "anywhere-computer"
        await session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = await session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert "documents_preview" in {tool["name"] for tool in tools["result"]["tools"]}
        packet = await session.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "documents_preview", "arguments": {
                **args(path).model_dump(), "request_id": uuid.uuid4().hex}},
        })
        assert packet["result"]["structuredContent"]["state"] == "completed"
        result = packet["result"]["structuredContent"]["data"]
        assert result["page"] == 1
        assert base64.b64decode(result["data_base64"])
        assert len(json.dumps(packet).encode()) < 8 * 1024 * 1024
    finally:
        await engine.close()


async def test_unavailable_renderer_has_safe_error_code_at_tool_boundary(tmp_path, monkeypatch):
    import anywhere_computer.document_preview as preview

    path = docx(tmp_path / "plain.docx")
    monkeypatch.setattr(preview.shutil, "which", lambda _name: None)
    engine = Engine(tmp_path / "engine")
    try:
        reply = await engine.execute(Request(
            operation_id=uuid.uuid4().hex, tool="documents_preview",
            arguments=args(path).model_dump()))
        assert reply.state == "failed"
        assert reply.data["error_code"] == "preview_unavailable"
        assert reply.data["dispatched"] is False
        assert "soffice" not in reply.model_dump_json()
    finally:
        await engine.close()


def test_preview_requires_current_hash_and_rejects_external_links(tmp_path):
    path = docx(tmp_path / "linked.docx", external=True)
    with pytest.raises(ValueError, match="Document changed"):
        preview_document(PreviewDocument(path=str(path), expected_sha256="0" * 64))
    with pytest.raises(ValueError, match="external relationships"):
        preview_document(args(path))


def test_missing_renderer_is_explicit(tmp_path, monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("DOCX rendering currently requires the macOS network sandbox")
    import anywhere_computer.document_preview as preview

    path = docx(tmp_path / "plain.docx")
    monkeypatch.setattr(preview.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="preview unavailable: soffice"):
        preview_document(args(path))


def test_preview_rejects_unsupported_platform_before_starting_renderer(tmp_path, monkeypatch):
    import anywhere_computer.document_preview as preview

    path = docx(tmp_path / "plain.docx")
    monkeypatch.setattr(preview.sys, "platform", "win32")
    with pytest.raises(ValueError, match="macOS network sandbox"):
        preview_document(args(path))


def test_sandbox_allows_only_temporary_output_writes(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS sandbox test")
    root = tmp_path / "sandbox"
    root.mkdir()
    policy = root / "network.sb"
    policy.write_text(_sandbox_policy(root))
    allowed = root / "inside"
    outside = tmp_path / "outside"
    base = [shutil.which("sandbox-exec"), "-f", str(policy), "/usr/bin/touch"]
    assert subprocess.run([*base, str(allowed)], capture_output=True).returncode == 0
    assert allowed.exists()
    assert subprocess.run([*base, str(outside)], capture_output=True).returncode != 0
    assert not outside.exists()


def test_renderer_file_output_is_limited(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS renderer process limit")
    target = tmp_path / "oversize"
    with pytest.raises(ValueError, match="renderer failed"):
        _run([sys.executable, "-c", "import pathlib,sys;"
              "pathlib.Path(sys.argv[1]).write_bytes(b'x'*4096)", str(target)],
             timeout=5, env=dict(os.environ), max_file_bytes=1024)
    assert target.stat().st_size <= 1024


def test_renderer_timeout_stops_child_process_group(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS renderer process group")
    child_pid = tmp_path / "child.pid"
    script = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
    )
    def alive(pid):
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    try:
        with pytest.raises(ValueError, match="timed out"):
            _run([sys.executable, "-c", script, str(child_pid)], timeout=1,
                 env=dict(os.environ))
        assert child_pid.exists()
        pid = int(child_pid.read_text())
        deadline = time.monotonic() + 3
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(pid)
    finally:
        if child_pid.exists():
            pid = int(child_pid.read_text())
            if alive(pid):
                psutil.Process(pid).kill()


async def test_renderer_concurrency_is_bounded_per_engine(tmp_path, monkeypatch):
    import anywhere_computer.engine as engine_module

    started = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    active = 0
    total_started = 0
    peak = 0

    def fake_preview(_args):
        nonlocal active, total_started, peak
        with guard:
            active += 1
            total_started += 1
            peak = max(peak, active)
            if total_started == 2:
                started.set()
        try:
            assert release.wait(3)
            return {"rendered": True}
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(engine_module, "preview_document", fake_preview)
    engine = Engine(tmp_path / "engine")
    requests = [Request(operation_id=uuid.uuid4().hex, tool="documents_preview",
                        arguments={"path": str(tmp_path / "test.docx"),
                                   "expected_sha256": "0" * 64}) for _ in range(3)]
    try:
        tasks = [asyncio.create_task(engine.execute(item)) for item in requests]
        assert await asyncio.to_thread(started.wait, 3)
        await asyncio.sleep(0.05)
        assert total_started == 2
        release.set()
        replies = await asyncio.gather(*tasks)
        assert all(reply.state == "completed" for reply in replies)
        assert total_started == 3 and peak == 2
    finally:
        release.set()
        await engine.close()


def test_preview_detects_source_change_during_conversion(tmp_path, monkeypatch):
    import anywhere_computer.document_preview as preview

    if any(shutil.which(name) is None for name in
           ("soffice", "pdfinfo", "pdftoppm", "sandbox-exec")):
        pytest.skip("Local sandboxed document renderer is unavailable")
    path = docx(tmp_path / "changing.docx")
    request = args(path)
    run = preview._run
    calls = 0

    def change_after_conversion(command, **kwargs):
        nonlocal calls
        result = run(command, **kwargs)
        calls += 1
        if calls == 1:
            path.write_bytes(path.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(preview, "_run", change_after_conversion)
    with pytest.raises(ValueError, match="changed during preview"):
        preview_document(request)
