import asyncio
import json

import pytest
from test_http_service import configured as configured
from test_remote_transport import certificates as certificates

from anywhere_computer.http_diagnostics import diagnose_http
from anywhere_computer.http_service import HTTPServiceConfig, http_service


async def test_http_doctor_tracks_real_service_without_authentication(configured, tmp_path):
    _, owner = configured
    assert (await diagnose_http(tmp_path))["state"] == "unreachable"
    async with http_service(tmp_path, credentials=owner):
        report = await diagnose_http(tmp_path)
        assert report["state"] == "metadata_reachable"
        assert report["authenticated"] is False
        assert report["public_reachability"] == "unverified"
        assert report["changed"] is False
    assert (await diagnose_http(tmp_path))["state"] == "unreachable"


async def test_http_doctor_missing_config_does_not_create_state(tmp_path):
    missing = tmp_path / "missing"
    assert (await diagnose_http(missing))["state"] == "configuration_unavailable"
    assert not missing.exists()


@pytest.mark.parametrize(
    "fault", ["other", "redirect", "oversize", "duplicate", "invalid", "stall"]
)
async def test_http_doctor_rejects_unrelated_or_unbounded_service(tmp_path, fault):
    requests = []
    finished = asyncio.Event()

    async def handler(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            if fault == "stall":
                await reader.read()
                return
            body = json.dumps({"resource": "https://other.example/mcp"}).encode()
            header = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            if fault == "redirect":
                header = b"HTTP/1.1 302 Found\r\nLocation: https://other.example/mcp\r\n"
            if fault == "invalid":
                body = b"[]"
            header += f"Content-Length: {20000 if fault == 'oversize' else len(body)}\r\n".encode()
            if fault == "duplicate":
                header += b"Content-Length: 0\r\n"
            writer.write(header + b"\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            finished.set()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        config = HTTPServiceConfig(
            resource="https://expected.example/mcp",
            owner="owner",
            client="client",
            device="a" * 32,
            scopes=frozenset({"files_read"}),
            redirects=frozenset({"https://client.example/callback"}),
            port=server.sockets[0].getsockname()[1],
        )
        directory = tmp_path / "http-server"
        directory.mkdir()
        (directory / "config.json").write_text(config.model_dump_json())
        report = await diagnose_http(tmp_path)
        expected = (
            "resource_mismatch"
            if fault == "other"
            else ("unreachable" if fault == "stall" else "unexpected_response")
        )
        assert report["state"] == expected
        assert report["authenticated"] is False and report["changed"] is False
        await asyncio.wait_for(finished.wait(), timeout=2)
        assert len(requests) == 1 and b"Authorization:" not in requests[0]
    finally:
        server.close()
        await server.wait_closed()


async def test_http_doctor_cli_exit_codes(configured, tmp_path):
    import subprocess
    import sys

    _, owner = configured
    command = [
        sys.executable,
        "-m",
        "anywhere_computer.cli",
        "http-doctor",
        "--state-dir",
        str(tmp_path),
    ]
    async with http_service(tmp_path, credentials=owner):
        ready = await asyncio.to_thread(
            subprocess.run, command, capture_output=True, text=True, timeout=10
        )
        assert ready.returncode == 0
        assert json.loads(ready.stdout)["state"] == "metadata_reachable"
    stopped = await asyncio.to_thread(
        subprocess.run, command, capture_output=True, text=True, timeout=10
    )
    assert stopped.returncode == 1
    assert json.loads(stopped.stdout)["state"] == "unreachable"


async def test_remote_doctor_default_never_uses_public_network(configured, tmp_path, monkeypatch):
    from anywhere_computer import http_diagnostics as diagnostic

    original = diagnostic._metadata
    calls = []

    async def metadata(port, *, resource=None):
        calls.append(resource)
        assert resource is None
        return await original(port)

    monkeypatch.setattr(diagnostic, "_metadata", metadata)
    _, owner = configured
    async with http_service(tmp_path, credentials=owner):
        report = await diagnostic.diagnose_remote(tmp_path)
    assert report["state"] == "local_metadata_reachable"
    assert report["public"] == {"state": "not_requested"}
    assert report["connector"]["process_state"] == "unverified"
    assert report["authenticated"] is False and report["changed"] is False
    assert calls == [None]


@pytest.mark.parametrize("available", [False, True])
async def test_remote_doctor_inspects_only_selected_connector(tmp_path, monkeypatch, available):
    from anywhere_computer import http_diagnostics as diagnostic

    selected = str(tmp_path / "接続子 with spaces")
    calls = []

    def lookup(value):
        calls.append(value)
        assert value == selected
        return selected if available else None

    monkeypatch.setattr(diagnostic.shutil, "which", lookup)
    report = await diagnostic.diagnose_remote(tmp_path, connector=selected)
    assert calls == [selected]
    assert report["connector"] == {
        "executable_available": available, "selection": "explicit_path",
        "version_state": "unverified", "process_state": "unverified",
    }
    assert report["changed"] is False


async def test_remote_doctor_rejects_relative_connector_before_probe(tmp_path, monkeypatch):
    from anywhere_computer import http_diagnostics as diagnostic

    async def unexpected_probe(directory):
        pytest.fail("Invalid connector must be rejected before metadata requests")

    monkeypatch.setattr(diagnostic, "diagnose_http", unexpected_probe)
    with pytest.raises(ValueError, match="absolute path"):
        await diagnostic.diagnose_remote(tmp_path, connector="relative-connector")


@pytest.mark.parametrize("public_state", ["ok", "mismatch", "certificate", "unreachable"])
async def test_remote_doctor_separates_local_and_public(configured, tmp_path, monkeypatch,
                                                       public_state):
    import ssl

    from anywhere_computer import http_diagnostics as diagnostic

    original = diagnostic._metadata
    config, owner = configured

    async def metadata(port, *, resource=None):
        if resource is None:
            return await original(port)
        assert resource == config.resource
        if public_state == "certificate":
            raise ssl.SSLCertVerificationError("synthetic certificate failure")
        if public_state == "unreachable":
            raise OSError("synthetic network failure")
        return {"resource": config.resource if public_state == "ok" else "https://other.example/mcp"}

    monkeypatch.setattr(diagnostic, "_metadata", metadata)
    async with http_service(tmp_path, credentials=owner):
        report = await diagnostic.diagnose_remote(tmp_path, probe_public=True)
    assert report["loopback"]["state"] == "metadata_reachable"
    assert report["public"]["state"] == {
        "ok": "metadata_reachable", "mismatch": "resource_mismatch",
        "certificate": "certificate_verification_failed", "unreachable": "unreachable",
    }[public_state]
    assert report["state"] == (
        "local_and_public_metadata_reachable" if public_state == "ok" else "attention_required"
    )


async def test_public_metadata_real_tls_verifies_hostname_and_bounds(certificates, monkeypatch):
    import ssl

    from anywhere_computer import http_diagnostics as diagnostic

    context, _ = certificates
    server_context = context("server", False)
    server_context.verify_mode = ssl.CERT_NONE
    requests = []
    response = {"status": 200, "size": None, "resource": "unset"}

    async def handler(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            body = json.dumps({"resource": response["resource"]}).encode()
            size = response["size"] or len(body)
            writer.write(
                f"HTTP/1.1 {response['status']} Test\r\nContent-Type: application/json\r\n"
                f"Content-Length: {size}\r\nLocation: https://not-followed.example/mcp\r\n\r\n"
                .encode() + body
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]
    resource = f"https://localhost:{port}/mcp"
    response["resource"] = resource
    try:
        # The disposable CA is not system-trusted. No insecure fallback is used.
        with pytest.raises(ssl.SSLCertVerificationError):
            await diagnostic._metadata(443, resource=resource)
        trusted = context("client", True)
        monkeypatch.setattr(diagnostic.ssl, "create_default_context", lambda: trusted)
        assert await diagnostic._metadata(443, resource=resource) == {"resource": resource}
        assert b"Authorization:" not in requests[-1] and b"Cookie:" not in requests[-1]
        with pytest.raises(ssl.SSLCertVerificationError):
            await diagnostic._metadata(443, resource=f"https://127.0.0.1:{port}/mcp")
        response["status"] = 302
        with pytest.raises(ValueError):
            await diagnostic._metadata(443, resource=resource)
        response.update(status=200, size=20000)
        with pytest.raises(ValueError):
            await diagnostic._metadata(443, resource=resource)
    finally:
        server.close()
        await server.wait_closed()


async def test_remote_doctor_missing_configuration_is_read_only(tmp_path):
    from anywhere_computer.http_diagnostics import diagnose_remote

    directory = tmp_path / "absent"
    report = await diagnose_remote(directory, probe_public=True)
    assert report["state"] == "configuration_unavailable"
    assert report["public"]["state"] == "not_requested"
    assert not directory.exists()
