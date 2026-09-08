from urllib.parse import urlencode

import httpx
import pytest
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.engine import Engine
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.oauth_endpoints import OAuthEndpoints

RESOURCE = "https://computer.example/mcp"
CALLBACK = "https://client.example/callback"
VERIFIER = "r" * 43


@pytest.fixture
async def oauth_server(tmp_path):
    engine = Engine(tmp_path / "agent")
    store = AuthorizationStore(
        tmp_path / "auth", resource=RESOURCE, known_tools=frozenset(engine.tools)
    )
    store.register_client("client", frozenset({CALLBACK}))
    store.enroll_device("owner", "device", frozenset({"computer_status"}))
    endpoints = OAuthEndpoints(store, authorization_endpoint="https://computer.example/authorize")
    backend = AuthorizedDeviceMCP(store, engine, owner="owner", device="device")
    adapter = HTTPMCP(
        backend.authenticate,
        backend.session,
        public_routes=endpoints.routes(),
        auth_challenge=endpoints.challenge,
    )
    port = await adapter.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            yield store, http
    finally:
        await adapter.close()
        store.close()
        await engine.close()


def request_form(store):
    code = store.approve(
        owner="owner",
        device="device",
        client="client",
        redirect=CALLBACK,
        resource=RESOURCE,
        tools=frozenset({"computer_status"}),
        challenge=pkce_s256(VERIFIER),
    )
    return {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": VERIFIER,
        "client_id": "client",
        "redirect_uri": CALLBACK,
        "resource": RESOURCE,
    }


async def test_discovery_code_exchange_and_authenticated_mcp(oauth_server):
    store, http = oauth_server
    challenge = await http.post("/mcp", json={})
    assert challenge.status_code == 401
    assert (
        'resource_metadata="https://computer.example/.well-known/oauth-protected-resource"'
        in challenge.headers["www-authenticate"]
    )
    resource = await http.get("/.well-known/oauth-protected-resource")
    metadata = ProtectedResourceMetadata.model_validate(resource.json())
    assert str(metadata.resource) == RESOURCE
    server = OAuthMetadata.model_validate(
        (await http.get("/.well-known/oauth-authorization-server")).json()
    )
    assert server.code_challenge_methods_supported == ["S256"]
    assert server.token_endpoint_auth_methods_supported == ["none"]
    assert (await http.get("/.well-known/oauth-protected-resource/mcp")).json() == resource.json()
    form = request_form(store)
    exchange = await http.post("/oauth/token", data=form)
    assert exchange.status_code == 200
    assert exchange.headers["cache-control"] == "no-store"
    assert exchange.headers["pragma"] == "no-cache"
    token = exchange.json()["access_token"]
    assert exchange.json()["scope"] == "computer_status"
    assert store.verify(token, resource=RESOURCE) is not None
    response = await http.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200 and "mcp-session-id" in response.headers
    replay = await http.post("/oauth/token", data=form)
    assert replay.status_code == 400 and replay.json() == {"error": "invalid_grant"}
    assert store.verify(token, resource=RESOURCE) is None


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"code_verifier": "wrong"}, "invalid_grant"),
        ({"client_id": "other"}, "invalid_grant"),
        ({"redirect_uri": CALLBACK + "/extra"}, "invalid_grant"),
        ({"resource": "https://other.example/mcp"}, "invalid_target"),
        ({"grant_type": "password"}, "unsupported_grant_type"),
        ({"client_secret": "not-supported"}, "invalid_client"),
        ({"code": ""}, "invalid_request"),
    ],
)
async def test_token_errors_are_bounded_and_do_not_consume_code(oauth_server, changes, expected):
    store, http = oauth_server
    form = request_form(store)
    failed = await http.post("/oauth/token", data={**form, **changes})
    assert failed.status_code == 400 and failed.json() == {"error": expected}
    assert failed.headers["pragma"] == "no-cache"
    assert (await http.post("/oauth/token", data=form)).status_code == 200


async def test_duplicate_malformed_and_oversized_form_rejected(oauth_server):
    store, http = oauth_server
    form = request_form(store)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    for body in (urlencode(form) + "&client_id=other", "code=%xx", "code=%FF", "x" * 16385):
        response = await http.post("/oauth/token", headers=headers, content=body)
        assert response.status_code in {400, 413}
        assert response.json() == {"error": "invalid_request"}
    assert (await http.post("/oauth/token", json=form)).status_code == 415
    assert (await http.get("/oauth/token")).status_code == 405
    assert (
        await http.post("/.well-known/oauth-authorization-server", data=form)
    ).status_code == 405
    assert (
        await http.get(
            "/.well-known/oauth-protected-resource", headers={"Origin": "https://evil.example"}
        )
    ).status_code == 403
    assert (await http.post("/oauth/token", data=form)).status_code == 200


async def test_http_refresh_keeps_mcp_session_and_replay_invalidates_it(oauth_server, monkeypatch):
    import time

    store, http = oauth_server
    first = (await http.post("/oauth/token", data=request_form(store))).json()
    headers = {
        "Authorization": f"Bearer {first['access_token']}",
        "Accept": "application/json, text/event-stream",
    }
    init = await http.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "refresh-test", "version": "1"},
            },
        },
    )
    headers["MCP-Session-Id"] = init.headers["mcp-session-id"]
    await http.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    now = time.time()
    monkeypatch.setattr("anywhere_computer.authorization.time.time", lambda: now + 901)
    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    assert (await http.post("/mcp", headers=headers, json=ping)).status_code == 401
    form = {
        "grant_type": "refresh_token",
        "refresh_token": first["refresh_token"],
        "client_id": "client",
        "resource": RESOURCE,
    }
    response = await http.post("/oauth/token", data=form)
    assert response.status_code == 200 and response.headers["pragma"] == "no-cache"
    renewed = response.json()
    headers["Authorization"] = f"Bearer {renewed['access_token']}"
    # Same MCP session ID, no reinitialization needed after ordinary renewal.
    assert (await http.post("/mcp", headers=headers, json=ping)).json()["result"] == {}
    assert (await http.post("/oauth/token", data=form)).json() == {"error": "invalid_grant"}
    assert (await http.post("/mcp", headers=headers, json=ping)).status_code == 401
