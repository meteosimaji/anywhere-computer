import asyncio
import dataclasses
import http.client
import io
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from test_client_tokens import MemoryVault
from test_remote_transport import certificates as certificates

from anywhere_computer import cli, http_client
from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.client_tokens import ClientAuthorizationRequired, ClientTokens, TokenReply
from anywhere_computer.engine import Engine
from anywhere_computer.http_client import HTTPBackend, HTTPResponse, https_mcp_request
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.mcp_server import OPERATION_META, PROTOCOL_VERSION, serve_stdio
from anywhere_computer.models import Request
from anywhere_computer.oauth_endpoints import OAuthEndpoints


@pytest.fixture
async def http_remote(tmp_path, request):
    resource = "https://computer.example/mcp"
    permissions = getattr(
        request,
        "param",
        frozenset({"computer_status", "files_read", "files_write", "operations_get"}),
    )
    engine = Engine(tmp_path / "agent")
    authority = AuthorizationStore(
        tmp_path / "authority", resource=resource, known_tools=permissions
    )
    callback = "https://client.example/callback"
    authority.register_client("client", frozenset({callback}))
    authority.enroll_device("owner", "device", permissions)
    code = authority.approve(
        owner="owner",
        device="device",
        client="client",
        redirect=callback,
        resource=resource,
        tools=permissions,
        challenge=pkce_s256("v" * 43),
    )
    started = time.time()
    issued = authority.exchange_code(
        code=code, verifier="v" * 43, client="client", redirect=callback, resource=resource
    )
    binding = AuthorizedDeviceMCP(authority, engine, owner="owner", device="device")
    oauth = OAuthEndpoints(authority, authorization_endpoint="https://computer.example/authorize")
    adapter = HTTPMCP(binding.authenticate, binding.session, public_routes=oauth.routes())
    port = await adapter.start()
    calls = []
    faults = {"lose_write_response": False, "refreshes": 0}

    def refresh(resource, client, token):
        faults["refreshes"] += 1
        result = httpx.post(
            f"http://127.0.0.1:{port}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": client,
                "resource": resource,
            },
        )
        if result.status_code != 200:
            raise ClientAuthorizationRequired("Synthetic authorization rejected")
        return TokenReply.model_validate(result.json())

    tokens = ClientTokens(
        tmp_path / "client",
        resource=resource,
        client="client",
        profile="device",
        vault=MemoryVault(),
        refresh=refresh,
    )
    tokens.install(
        TokenReply(
            access_token=issued.value,
            refresh_token=issued.refresh_value,
            scope=issued.scope,
            expires_in=issued.expires_in,
            token_type="Bearer",
        ),
        requested_at=started,
    )

    def wire(resource, method, packet, headers):
        assert resource == "https://computer.example/mcp"
        if packet is not None and packet.get("method") != "initialize":
            assert headers["MCP-Protocol-Version"] == PROTOCOL_VERSION
            assert headers.get("MCP-Session-Id")
        calls.append(packet)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(
                method,
                "/mcp",
                body=json.dumps(packet).encode() if packet else None,
                headers=headers,
            )
            response = connection.getresponse()
            raw = response.read()
            result = HTTPResponse(
                response.status,
                {key.lower(): value for key, value in response.getheaders()},
                json.loads(raw) if raw else None,
            )
        finally:
            connection.close()
        if (
            faults["lose_write_response"]
            and packet
            and packet.get("method") == "tools/call"
            and packet["params"]["name"] in {"files_write", "files_write_binary"}
        ):
            faults["lose_write_response"] = False
            raise ConnectionError("Simulated loss after server execution")
        return result

    backend = HTTPBackend(tokens, wire=wire)
    try:
        yield backend, adapter, authority, engine, calls, faults
    finally:
        await backend.close()
        await adapter.close()
        authority.close()
        await engine.close()


def operation(tool, **arguments):
    return Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)


async def test_http_connector_recovers_sessions_and_keeps_operation_ids(http_remote, tmp_path):
    backend, adapter, _, _, calls, _ = http_remote
    assert {item["name"] for item in await backend.catalog()} == {
        "computer_status",
        "files_read",
        "files_write",
        "operations_get",
    }
    old_session = backend.session_id
    adapter.sessions.clear()
    write = operation("files_write", path=str(tmp_path / "remote.txt"), text="remote 日本語")
    written = await backend.execute(write)
    assert written.state == "completed" and written.operation_id == write.operation_id
    assert old_session != backend.session_id
    reads = await backend.execute(operation("files_read", path=str(tmp_path / "remote.txt")))
    assert reads.data["text"] == "remote 日本語"
    attempted = [
        packet
        for packet in calls
        if packet
        and packet.get("method") == "tools/call"
        and packet["params"]["name"] == "files_write"
    ]
    assert len(attempted) == 2  # First one was rejected with 404 before dispatch.
    assert {p["params"]["_meta"][OPERATION_META] for p in attempted} == {write.operation_id}


async def test_lost_write_response_is_not_retried_and_can_be_looked_up(http_remote, tmp_path):
    backend, adapter, _, engine, calls, faults = http_remote
    await backend.catalog()
    original_session = backend.session_id
    target = tmp_path / "lost.txt"
    write = operation("files_write", path=str(target), text="one effect")
    faults["lose_write_response"] = True
    unknown = await backend.execute(write)
    assert unknown.state == "unknown" and unknown.operation_id == write.operation_id
    assert backend.session_id == original_session
    assert target.read_text(encoding="utf-8") == "one effect"
    assert len([p for p in calls if p and p.get("method") == "tools/call"]) == 1
    looked_up = await backend.execute(operation("operations_get", operation_id=write.operation_id))
    assert looked_up.state == "completed" and looked_up.data["state"] == "completed"
    assert looked_up.data["operation_id"] == write.operation_id
    # Explicit same-ID redelivery reaches the existing ledger result, not a new write.
    assert (await backend.execute(write)).model_dump() == (
        await backend.execute(write)
    ).model_dump()
    assert target.read_text(encoding="utf-8") == "one effect"
    conflict = write.model_copy(update={"arguments": {"path": str(target), "text": "different"}})
    assert (await backend.execute(conflict)).state == "failed"
    assert target.read_text(encoding="utf-8") == "one effect"
    assert (
        engine.ledger.connection.execute(
            "SELECT count(*) FROM operations WHERE tool='files_write'"
        ).fetchone()[0]
        == 1
    )
    await backend.close()
    assert not adapter.sessions


async def test_explicit_401_renews_once_and_revocation_never_dispatches(http_remote, tmp_path):
    backend, _, authority, _, calls, faults = http_remote
    await backend.catalog()
    authority.db.execute("UPDATE tokens SET expires=0")
    authority.db.commit()
    write = operation("files_write", path=str(tmp_path / "renewed.txt"), text="after renewal")
    assert (await backend.execute(write)).state == "completed"
    assert faults["refreshes"] == 1
    assert (
        await backend.execute(operation("files_read", path=str(tmp_path / "renewed.txt")))
    ).state == "completed"
    assert faults["refreshes"] == 1
    authority.revoke_device(owner="owner", device="device")
    denied_path = tmp_path / "denied.txt"
    denied = await backend.execute(operation("files_write", path=str(denied_path), text="denied"))
    assert denied.state == "failed" and not denied_path.exists()
    assert faults["refreshes"] == 2
    count = len(calls)
    assert (
        await backend.execute(operation("files_write", path=str(denied_path), text="denied"))
    ).state == "failed"
    assert len(calls) == count  # Pending refresh does not loop on invalid_grant.


async def test_stdio_proxy_preserves_known_operation_id_on_loss(http_remote, tmp_path):
    backend, _, _, _, _, faults = http_remote
    session = backend.mcp_session()
    await session.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {},
            },
        }
    )
    await session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    faults["lose_write_response"] = True
    source = io.BytesIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "files_write",
                    "arguments": {"path": str(tmp_path / "stdio.txt"), "text": "one"},
                },
            }
        ).encode()
        + b"\n"
    )
    destination = io.BytesIO()
    await serve_stdio(session, source, destination)
    reply = json.loads(destination.getvalue())["result"]["structuredContent"]
    assert reply["state"] == "unknown"
    lookup = await backend.execute(operation("operations_get", operation_id=reply["operation_id"]))
    assert lookup.data["state"] == "completed"


async def test_invalid_meta_id_rejected_without_dispatch(http_remote):
    backend, _, _, _, calls, _ = http_remote
    session = backend.mcp_session()
    await session.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {},
            },
        }
    )
    await session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    for invalid in (None, True, "short", "a" * 33, "A" * 32):
        result = await session.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "computer_status",
                    "arguments": {},
                    "_meta": {OPERATION_META: invalid},
                },
            }
        )
        assert result["error"]["code"] == -32602
    assert not any(p.get("method") == "tools/call" for p in calls if p)


def test_http_cli_requires_public_connection_fields(monkeypatch, capsys):
    for args in (("http-mcp",), ("status", "--resource", "https://example.com/mcp")):
        monkeypatch.setattr(sys, "argv", ["anywhere", *args])
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
        assert not capsys.readouterr().out


def test_https_json_and_sse_transport(certificates, monkeypatch):
    context, _ = certificates
    request = {"jsonrpc": "2.0", "id": "one", "method": "ping"}
    body = json.dumps({"jsonrpc": "2.0", "id": "one", "result": {}}).encode()
    configured = {"status": 200, "type": "application/json", "body": body}
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(configured["status"])
            self.send_header("Content-Type", configured["type"])
            self.send_header("Location", "https://not-followed.example/mcp")
            self.end_headers()
            self.wfile.write(configured["body"])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context("server", False).wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    trusted = context("client", True)
    monkeypatch.setattr(http_client.ssl, "create_default_context", lambda: trusted)
    resource = f"https://localhost:{server.server_port}/mcp"
    try:
        assert https_mcp_request(resource, "POST", request, {}).packet["result"] == {}
        for newline in (b"\n", b"\r\n", b"\r"):
            configured.update(
                type="text/event-stream",
                body=newline.join(
                    [
                        b"id: priming",
                        b"data:",
                        b"",
                        b": keepalive",
                        b"",
                        b'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}',
                        b"",
                        b"data: " + body,
                        b"",
                        b"",
                    ]
                ),
            )
            assert https_mcp_request(resource, "POST", request, {}).packet["id"] == "one"
        configured.update(status=302, body=b"secret-looking error page")
        before = len(calls)
        assert https_mcp_request(resource, "POST", request, {}).status == 302
        assert len(calls) == before + 1
        configured.update(status=200, type="application/json", body=b"not-json")
        with pytest.raises(ConnectionError, match="not confirmed"):
            https_mcp_request(resource, "POST", request, {})
        before = len(calls)
        with pytest.raises(ConnectionError):
            https_mcp_request(resource.replace("localhost", "127.0.0.1"), "POST", request, {})
        assert len(calls) == before
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


async def test_cancelled_observer_does_not_release_inflight_transport_lock(http_remote, tmp_path):
    backend, _, _, engine, _, _ = http_remote
    await backend.catalog()
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine.tools["files_write"]

    async def delayed(args):
        entered.set()
        await release.wait()
        return await original.handler(args)

    engine.tools["files_write"] = dataclasses.replace(original, handler=delayed)
    write = operation("files_write", path=str(tmp_path / "cancelled.txt"), text="preserved")
    observer = asyncio.create_task(backend.execute(write))
    await asyncio.wait_for(entered.wait(), 3)
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    lookup = asyncio.create_task(
        backend.execute(operation("operations_get", operation_id=write.operation_id))
    )
    try:
        await asyncio.sleep(0.03)
        assert not lookup.done()
    finally:
        release.set()
    result = await asyncio.wait_for(lookup, 3)
    assert result.data["state"] == "completed"
    assert (tmp_path / "cancelled.txt").read_text(encoding="utf-8") == "preserved"


@pytest.mark.parametrize("fault", [
    "wrong-rpc-id", "wrong-operation-id", "dispatch-timeout", "upstream-unavailable",
])
async def test_unconfirmed_responses_never_become_failed_or_replayed(http_remote, tmp_path, fault):
    backend, _, _, _, calls, _ = http_remote
    await backend.catalog()
    original = backend.wire

    def broken(resource, method, packet, headers):
        response = original(resource, method, packet, headers)
        if packet and packet.get("method") == "tools/call":
            if fault in {"dispatch-timeout", "upstream-unavailable"}:
                return HTTPResponse(408 if fault == "dispatch-timeout" else 503, {}, None)
            body = dict(response.packet)
            if fault == "wrong-rpc-id":
                body["id"] = "different"
            else:
                body["result"]["structuredContent"]["operation_id"] = "different"
            return HTTPResponse(response.status, response.headers, body)
        return response

    backend.wire = broken
    write = operation("files_write", path=str(tmp_path / "unconfirmed.txt"), text="once")
    result = await backend.execute(write)
    assert result.state == "unknown" and result.operation_id == write.operation_id
    assert (tmp_path / "unconfirmed.txt").read_text(encoding="utf-8") == "once"
    assert len([p for p in calls if p and p.get("method") == "tools/call"]) == 1
    backend.wire = original
    recovered = await backend.execute(operation("operations_get", operation_id=write.operation_id))
    assert recovered.data["state"] == "completed"
    assert recovered.data["operation_id"] == write.operation_id
    writes = [p for p in calls if p and p.get("method") == "tools/call"
              and p["params"]["name"] == "files_write"]
    assert len(writes) == 1


async def test_server_without_operation_extension_cannot_receive_tool_calls(http_remote, tmp_path):
    backend, _, _, _, calls, _ = http_remote
    original = backend.wire

    def legacy(resource, method, packet, headers):
        response = original(resource, method, packet, headers)
        if packet and packet.get("method") == "initialize":
            response.packet["result"]["capabilities"].pop("experimental")
        return response

    backend.wire = legacy
    path = tmp_path / "not-dispatched.txt"
    result = await backend.execute(operation("files_write", path=str(path), text="denied"))
    assert result.state == "failed" and not path.exists()
    assert not any(p.get("method") == "tools/call" for p in calls if p)


@pytest.mark.parametrize("expirations", [1, 2])
async def test_session_expiry_during_initialization_is_recovered_once(http_remote, expirations):
    backend, adapter, _, _, calls, _ = http_remote
    remaining = expirations
    original = adapter._dispatch

    async def expire_before_ack(method, headers, body):
        nonlocal remaining
        packet = json.loads(body) if body else {}
        if packet.get("method") == "notifications/initialized" and remaining:
            remaining -= 1
            adapter.sessions.clear()
        return await original(method, headers, body)

    adapter._dispatch = expire_before_ack
    result = await backend.execute(operation("computer_status"))
    assert result.state == ("completed" if expirations == 1 else "failed")
    assert sum(p.get("method") == "initialize" for p in calls if p) == 2
    assert sum(p.get("method") == "tools/call" for p in calls if p) == (expirations == 1)
