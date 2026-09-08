import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_client_tokens import MemoryVault
from test_http_mcp import INITIALIZE

from anywhere_computer import cli
from anywhere_computer.authorization import pkce_s256
from anywhere_computer.client_tokens import ClientCredentialError
from anywhere_computer.http_service import (
    configure_http,
    http_service,
    load_http_config,
    revoke_http_device,
)
from anywhere_computer.owner_credentials import OwnerCredentials

RESOURCE = "https://computer.example/mcp"
REDIRECT = "https://client.example/callback"
SCOPES = frozenset({"files_read", "files_write", "operations_get"})


async def setup(directory, port, **changes):
    options = dict(
        resource=RESOURCE,
        owner="owner",
        client="native",
        port=port,
        scopes=SCOPES,
        redirects=frozenset({REDIRECT}),
    )
    return await configure_http(directory, **{**options, **changes})


@pytest.fixture
async def configured(tmp_path, unused_tcp_port):
    config = await setup(tmp_path, unused_tcp_port)
    owner = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    owner.initialize("synthetic owner password")
    return config, owner


async def authenticate(http):
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "native",
            "redirect_uri": REDIRECT,
            "resource": RESOURCE,
            "scope": " ".join(sorted(SCOPES)),
            "state": "client-state",
            "code_challenge": pkce_s256("v" * 43),
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 200
    fields = dict(re.findall(r"name=(request_id|csrf) value='([^']+)'", response.text))
    response = await http.post(
        "/authorize",
        data={**fields, "approve": "yes", "password": "synthetic owner password"},
        headers={
            "Origin": "https://computer.example",
            "Cookie": response.headers["set-cookie"].split(";", 1)[0],
        },
    )
    assert response.status_code == 303
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    response = await http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": "v" * 43,
            "client_id": "native",
            "redirect_uri": REDIRECT,
            "resource": RESOURCE,
        },
    )
    assert response.status_code == 200
    return response.json()["access_token"]


async def initialize(http, token):
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
    response = await http.post("/mcp", headers=headers, json=INITIALIZE)
    assert response.status_code == 200
    headers.update(
        {"MCP-Session-Id": response.headers["mcp-session-id"], "MCP-Protocol-Version": "2025-11-25"}
    )
    response = await http.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert response.status_code == 202
    return headers


async def test_server_restart_preserves_operations_and_revocation(configured, tmp_path):
    config, owner = configured
    target = tmp_path / "test.txt"
    request = {
        "jsonrpc": "2.0",
        "id": "write",
        "method": "tools/call",
        "params": {
            "name": "files_write",
            "arguments": {"path": str(target), "text": "first"},
            "_meta": {"io.github.meteosimaji.anywhere-computer/operation_id": "a" * 32},
        },
    }
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{config.port}", trust_env=False
    ) as http:
        async with http_service(tmp_path, credentials=owner):
            token = await authenticate(http)
            headers = await initialize(http, token)
            response = await http.post("/mcp", headers=headers, json=request)
            assert response.status_code == 200 and not response.json()["result"]["isError"]
            assert target.read_text() == "first"
        target.write_text("external change")
        async with http_service(tmp_path, credentials=owner):
            headers = await initialize(http, token)
            response = await http.post("/mcp", headers=headers, json=request)
            assert response.status_code == 200 and not response.json()["result"]["isError"]
            assert target.read_text() == "external change"
            revoke_http_device(tmp_path)
            assert (await http.post("/mcp", headers=headers, json=INITIALIZE)).status_code == 401
        async with http_service(tmp_path, credentials=owner):
            assert (await http.post("/mcp", headers=headers, json=INITIALIZE)).status_code == 401
    assert load_http_config(tmp_path) == config
    assert not (tmp_path / "operations.sqlite3").exists()
    assert (tmp_path / "http-server/engine/operations.sqlite3").exists()


async def test_service_lock_and_configuration_are_not_replaceable(configured, tmp_path):
    config, owner = configured
    async with http_service(tmp_path, credentials=owner):
        with pytest.raises(TimeoutError):
            async with http_service(tmp_path, credentials=owner):
                pytest.fail("Duplicate service started")
        with pytest.raises(TimeoutError):
            await setup(tmp_path, config.port)
    with pytest.raises(ValueError, match="already configured"):
        await setup(tmp_path, config.port)
    assert load_http_config(tmp_path) == config


async def test_invalid_setup_does_not_publish_partial_configuration(tmp_path, unused_tcp_port):
    with pytest.raises(ValueError):
        await setup(tmp_path, unused_tcp_port, scopes=frozenset({"no_such_tool"}))
    assert not (tmp_path / "http-server").exists()
    assert not list(tmp_path.glob(".http-setup-*"))
    assert (await setup(tmp_path, unused_tcp_port)).resource == RESOURCE


async def test_missing_owner_fails_before_opening_server(tmp_path, unused_tcp_port):
    await setup(tmp_path, unused_tcp_port)
    owner = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    with pytest.raises(ClientCredentialError, match="not been initialized"):
        async with http_service(tmp_path, credentials=owner):
            pytest.fail("Uninitialized owner was accepted")


async def test_credentials_from_another_state_directory_are_rejected(configured, tmp_path):
    owner = OwnerCredentials(
        tmp_path / "other", resource=RESOURCE, owner="owner", vault=MemoryVault()
    )
    with pytest.raises(ValueError, match="do not match"):
        async with http_service(tmp_path, credentials=owner):
            pytest.fail("Another state directory's owner was accepted")


@pytest.mark.parametrize("damage", ["missing-db", "config-scopes", "config-client"])
async def test_lost_or_mismatched_authorization_is_not_recreated(configured, tmp_path, damage):
    _, owner = configured
    database = tmp_path / "http-server/authorization/authorization.sqlite3"
    if damage == "missing-db":
        database.unlink()
    else:
        path = tmp_path / "http-server/config.json"
        config = json.loads(path.read_text())
        config["scopes" if damage == "config-scopes" else "client"] = (
            ["files_read"] if damage == "config-scopes" else "other"
        )
        path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        async with http_service(tmp_path, credentials=owner):
            pytest.fail("Damaged authorization configuration started")
    if damage == "missing-db":
        assert not database.exists()


def test_http_cli_configuration_show_and_revoke(tmp_path, unused_tcp_port, monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "anywhere",
            "http-configure",
            "--state-dir",
            str(tmp_path),
            "--resource",
            RESOURCE,
            "--owner",
            "owner",
            "--client-id",
            "native",
            "--scope",
            "files_read",
            "--port",
            str(unused_tcp_port),
        ],
    )
    cli.main()
    configured = json.loads(capsys.readouterr().out)
    assert set(configured["redirects"]) == {
        "http://127.0.0.1/oauth/callback",
        "http://[::1]/oauth/callback",
    }
    monkeypatch.setattr("sys.argv", ["anywhere", "http-show", "--state-dir", str(tmp_path)])
    cli.main()
    assert json.loads(capsys.readouterr().out) == configured
    monkeypatch.setattr("sys.argv", ["anywhere", "http-revoke", "--state-dir", str(tmp_path)])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"http_device_revoked": True}


async def test_busy_port_releases_service_lock_for_retry(configured, tmp_path):
    import asyncio

    config, owner = configured
    blocker = await asyncio.start_server(
        lambda reader, writer: writer.close(), "127.0.0.1", config.port
    )
    try:
        with pytest.raises(OSError):
            async with http_service(tmp_path, credentials=owner):
                pytest.fail("Occupied port was reused")
    finally:
        blocker.close()
        await blocker.wait_closed()
    async with http_service(tmp_path, credentials=owner):
        pass
