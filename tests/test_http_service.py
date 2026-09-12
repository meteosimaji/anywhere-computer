import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_client_tokens import MemoryVault
from test_http_mcp import INITIALIZE

from anywhere_computer import cli
from anywhere_computer import http_service as service_module
from anywhere_computer.authorization import pkce_s256
from anywhere_computer.client_tokens import ClientCredentialError
from anywhere_computer.engine import Engine
from anywhere_computer.files import sha256
from anywhere_computer.http_service import (
    configure_http,
    enable_http_device,
    http_authorization_status,
    http_service,
    load_http_config,
    revoke_http_device,
)
from anywhere_computer.locking import ProcessLock
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


async def authenticate(http, *, client_id="native", redirect=REDIRECT):
    metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["authorization_response_iss_parameter_supported"] is True
    resource_metadata = (await http.get("/.well-known/oauth-protected-resource")).json()
    assert resource_metadata["authorization_servers"] == [metadata["issuer"]]
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
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
    fields = parse_qs(urlsplit(response.headers["location"]).query)
    assert fields["iss"] == [metadata["issuer"]]
    assert response.headers["location"].split("?", 1)[0] == redirect
    code = fields["code"][0]
    response = await http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": "v" * 43,
            "client_id": client_id,
            "redirect_uri": redirect,
            "resource": RESOURCE,
        },
    )
    assert response.status_code == 200
    return response.json()["access_token"]


async def test_chatgpt_predefined_client_uses_stable_issuer_callback(tmp_path, unused_tcp_port):
    from anywhere_computer.remote_setup import CHATGPT_CLIENT, CHATGPT_REDIRECT

    config = await setup(tmp_path, unused_tcp_port, client=CHATGPT_CLIENT,
                         redirects=frozenset({CHATGPT_REDIRECT}))
    owner = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    owner.initialize("synthetic owner password")
    async with http_service(tmp_path, credentials=owner):
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{config.port}", trust_env=False,
        ) as http:
            token = await authenticate(http, client_id=CHATGPT_CLIENT, redirect=CHATGPT_REDIRECT)
            headers = await initialize(http, token)
            result = await http.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": "catalog", "method": "tools/list",
            })
            assert {row["name"] for row in result.json()["result"]["tools"]} == SCOPES


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


async def test_live_reenable_requires_new_login_and_survives_restart(configured, tmp_path):
    config, owner = configured
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{config.port}", trust_env=False
    ) as http:
        async with http_service(tmp_path, credentials=owner):
            old_token = await authenticate(http)
            old_headers = await initialize(http, old_token)
            revoke_http_device(tmp_path)
            assert http_authorization_status(tmp_path) == {
                "device_id": config.device,
                "device_enabled": False,
            }
            assert enable_http_device(tmp_path)
            assert http_authorization_status(tmp_path)["device_enabled"]
            catalog = {"jsonrpc": "2.0", "id": "catalog", "method": "tools/list"}
            assert (await http.post("/mcp", headers=old_headers, json=catalog)).status_code == 401
            new_token = await authenticate(http)
            mixed_headers = {**old_headers, "Authorization": "Bearer " + new_token}
            assert (await http.post("/mcp", headers=mixed_headers, json=catalog)).status_code == 404
            new_headers = await initialize(http, new_token)
            assert not enable_http_device(tmp_path)
            assert (await http.post("/mcp", headers=new_headers, json=catalog)).status_code == 200
        async with http_service(tmp_path, credentials=owner):
            assert (
                await http.post("/mcp", headers=old_headers, json=INITIALIZE)
            ).status_code == 401
            await initialize(http, new_token)


async def test_enable_and_status_refuse_missing_or_mismatched_database(configured, tmp_path):
    config, _ = configured
    path = tmp_path / "http-server/config.json"
    data = config.model_dump(mode="json")
    data["client"] = "unregistered"
    path.write_text(json.dumps(data))
    for operation in (enable_http_device, http_authorization_status):
        with pytest.raises(ValueError, match="differs"):
            operation(tmp_path)
    path.write_text(config.model_dump_json())
    database = tmp_path / "http-server/authorization/authorization.sqlite3"
    database.unlink()
    for operation in (enable_http_device, revoke_http_device, http_authorization_status):
        with pytest.raises(ValueError, match="missing"):
            operation(tmp_path)
        assert not database.exists()


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
    monkeypatch.setattr("sys.argv", ["anywhere", "http-auth-status", "--state-dir", str(tmp_path)])
    cli.main()
    assert json.loads(capsys.readouterr().out)["device_enabled"] is False
    monkeypatch.setattr("sys.argv", ["anywhere", "http-enable", "--state-dir", str(tmp_path)])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"http_device_enabled": True, "changed": True}
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"http_device_enabled": True, "changed": False}


async def test_busy_port_releases_service_lock_for_retry(configured, tmp_path):
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


async def test_http_and_local_engines_share_write_lock_without_sharing_ledger(
    configured, tmp_path, monkeypatch
):
    _, owner = configured
    local = Engine(tmp_path)
    created = []

    def create_engine(*args, **kwargs):
        engine = Engine(*args, **kwargs)
        created.append(engine)
        return engine

    monkeypatch.setattr(service_module, "Engine", create_engine)
    try:
        async with http_service(tmp_path, credentials=owner):
            remote = created[0]
            assert remote.ledger is not local.ledger
            assert remote.files.backups != local.files.backups
            name = sha256(str((tmp_path / "shared.txt").resolve()).encode())
            with ProcessLock(local.files.locks / name):
                with pytest.raises(TimeoutError):
                    with ProcessLock(remote.files.locks / name):
                        pytest.fail("HTTP and local writes did not share their lock")
    finally:
        await local.close()


async def test_shared_http_restart_keeps_agent_and_deduplication(configured, tmp_path, monkeypatch):
    from anywhere_computer.connection import exchange, serve

    config, owner = configured
    directory = tmp_path / 'shared'
    selected = config.model_copy(update={'shared_agent_directory': str(directory)})
    (tmp_path / 'http-server/config.json').write_text(selected.model_dump_json())
    stop = asyncio.Event()
    monkeypatch.setattr('anywhere_computer.connection.local_credential',
                        lambda *a, **kw: 'shared-service-fixture')
    agent = asyncio.create_task(serve(
        directory, credential='shared-service-fixture', shutdown=stop,
    ))
    try:
        async with asyncio.timeout(5):
            while not (directory / 'agent.json').exists():
                if agent.done():
                    await agent
                await asyncio.sleep(0.01)
        before = await exchange(directory, '__status')

        def forbidden_engine(*a, **kw):
            pytest.fail('Shared HTTP frontend must not instantiate its own Engine')

        monkeypatch.setattr(service_module, 'Engine', forbidden_engine)
        target = tmp_path / 'shared-result.txt'
        request = {'jsonrpc': '2.0', 'id': 'write', 'method': 'tools/call', 'params': {
            'name': 'files_write', 'arguments': {
                'request_id': 'c' * 32, 'path': str(target), 'text': 'first',
            },
        }}
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{config.port}',
                                     trust_env=False) as http:
            async with http_service(tmp_path, credentials=owner):
                token = await authenticate(http)
                headers = await initialize(http, token)
                result = (await http.post('/mcp', headers=headers, json=request)).json()
                assert result['result']['structuredContent']['state'] == 'completed'
            # Closing HTTP must leave the separately owned agent alive.
            after = await exchange(directory, '__status')
            assert after.data['instance_id'] == before.data['instance_id']
            target.write_text('external change')
            async with http_service(tmp_path, credentials=owner):
                headers = await initialize(http, token)
                result = (await http.post('/mcp', headers=headers, json=request)).json()
                assert result['result']['structuredContent']['state'] == 'completed'
                assert target.read_text() == 'external change'
    finally:
        stop.set()
        await asyncio.wait_for(agent, 10)
