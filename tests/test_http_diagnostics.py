import asyncio
import json

import pytest
from test_http_service import configured as configured

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
