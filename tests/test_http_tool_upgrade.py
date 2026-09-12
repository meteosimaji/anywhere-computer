import json

import httpx
import pytest
from test_client_tokens import MemoryVault
from test_http_service import RESOURCE, authenticate, initialize, setup

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.http_service import http_service, load_http_config
from anywhere_computer.http_tool_upgrade import add_http_tools
from anywhere_computer.owner_credentials import OwnerCredentials


async def test_upgrade_preserves_token_and_adds_direct_tools_over_real_http(
    tmp_path, unused_tcp_port
):
    config = await setup(tmp_path, unused_tcp_port)
    owner = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    owner.initialize("synthetic owner password")
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{config.port}") as http:
        async with http_service(tmp_path, credentials=owner):
            token = await authenticate(http)
            with pytest.raises(TimeoutError):
                await add_http_tools(tmp_path, frozenset({"mcp_tools"}))
        result = await add_http_tools(tmp_path, frozenset({"mcp_tools"}))
        assert result["expanded_full_access_grants"] == 1
        assert result["credentials_replaced"] is False
        async with http_service(tmp_path, credentials=owner):
            headers = await initialize(http, token)
            response = await http.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/list",
                },
            )
            assert {t["name"] for t in response.json()["result"]["tools"]} == (
                config.scopes | {"mcp_tools"}
            )
        assert (await add_http_tools(tmp_path, frozenset({"mcp_tools"})))[
            "expanded_full_access_grants"
        ] == 0


async def test_upgrade_does_not_expand_restricted_revoked_or_expired_grants(
    tmp_path, unused_tcp_port
):
    config = await setup(tmp_path, unused_tcp_port)
    store = AuthorizationStore(
        tmp_path / "http-server/authorization", resource=RESOURCE, known_tools=config.scopes
    )
    try:
        for kind, scopes in [
            ("restricted", frozenset({"files_read"})),
            ("revoked", config.scopes),
            ("expired", config.scopes),
        ]:
            store.approve(
                owner=config.owner,
                device=config.device,
                client=config.client,
                redirect=next(iter(config.redirects)),
                resource=RESOURCE,
                tools=scopes,
                challenge=pkce_s256("v" * 43),
            )
            grant = store.db.execute(
                "SELECT id FROM grants ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
            if kind == "revoked":
                store.revoke(owner=config.owner, grant=grant)
            if kind == "expired":
                with store.db:
                    store.db.execute("UPDATE grants SET expires=1 WHERE id=?", (grant,))
        before = store.db.execute("SELECT id,tools,expires,revoked FROM grants").fetchall()
    finally:
        store.close()
    assert (await add_http_tools(tmp_path, frozenset({"mcp_tools"})))[
        "expanded_full_access_grants"
    ] == 0
    store = AuthorizationStore(
        tmp_path / "http-server/authorization",
        resource=RESOURCE,
        known_tools=config.scopes | {"mcp_tools"},
    )
    try:
        assert store.db.execute("SELECT id,tools,expires,revoked FROM grants").fetchall() == before
    finally:
        store.close()


async def test_interrupted_config_publication_is_recoverable_by_same_upgrade(
    tmp_path,
    unused_tcp_port,
    monkeypatch,
):
    from anywhere_computer import http_tool_upgrade

    config = await setup(tmp_path, unused_tcp_port)
    replace = http_tool_upgrade.os.replace

    def fail_publication(source, destination):
        if str(destination).endswith("http-server/config.json"):
            raise OSError("synthetic failure")
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(http_tool_upgrade.os, "replace", fail_publication)
        with pytest.raises(OSError, match="synthetic failure"):
            await add_http_tools(tmp_path, frozenset({"mcp_tools"}))
    assert load_http_config(tmp_path) == config
    await add_http_tools(tmp_path, frozenset({"mcp_tools"}))
    assert load_http_config(tmp_path).scopes == config.scopes | {"mcp_tools"}
    assert not list((tmp_path / "http-server").glob(".config-tools-*"))
    with pytest.raises(ValueError):
        await add_http_tools(tmp_path, frozenset({"operations_recent"}))
    with pytest.raises(ValueError):
        await add_http_tools(tmp_path, frozenset({"invented_tool"}))
    assert json.loads((tmp_path / "http-server/config.json").read_text())["resource"] == RESOURCE
