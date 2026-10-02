"""Publication ceilings use disposable state and synthetic local authorization only."""

import asyncio
import hashlib
import json
import sys
from contextlib import asynccontextmanager

import httpx
import pytest
from test_client_tokens import MemoryVault
from test_http_service import RESOURCE, SCOPES, authenticate, initialize, setup

from anywhere_computer import cli, connection, remote_setup
from anywhere_computer import http_service as service_module
from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.engine import Engine
from anywhere_computer.http_service import (
    HTTPServiceConfig,
    http_service,
    load_http_config,
    save_http_config,
)
from anywhere_computer.http_tool_profile import PUBLIC_CORE_TOOLS
from anywhere_computer.http_tool_upgrade import add_http_tools
from anywhere_computer.models import Empty
from anywhere_computer.owner_credentials import OwnerCredentials
from anywhere_computer.setup_connector import ConnectionSetupDraft
from anywhere_computer.setup_controller import SetupController
from anywhere_computer.subchat_gateway import SubchatGatewayConfig

PRIVATE_TOOLS = [
    "subchat_status", "subchat_send", "subchat_save_file", "subchat_queue_auto",
    "codex_threads_list", "codex_thread_read", "codex_skills_list", "codex_skill_read",
    "codex_plugin_tools", "codex_plugin_call", "codex_plugin_session_open",
    "mcp_session_open", "mcp_tools", "mcp_call", "mcp_watch_list",
    "devices_list", "devices_probe", "devices_tools", "devices_call",
    "gui_native_windows", "gui_observe", "skills_list", "settings_update",
    "operations_recent", "files_future_private_fixture", "documents_edit_cell",
]
FUTURE_TOOL = "files_future_private_fixture"


def selected_subchat(tmp_path):
    return SubchatGatewayConfig(
        profile=str(tmp_path / "fixture-profile"), ledger=str(tmp_path / "fixture-ledger"),
        account_id="fixture-account", consent="ordinary-chat-browser-control-approved",
    )


def public_config(**changes):
    return HTTPServiceConfig(**{
        "resource": RESOURCE, "owner": "owner", "client": "native", "device": "a" * 32,
        "port": 18768, "scopes": SCOPES, "redirects": frozenset({"https://client.example/callback"}),
        "tool_profile": "public-core", **changes,
    })


async def test_new_registered_tool_does_not_expand_public_core(monkeypatch):
    dispatched = []

    class UpdatedEngine(Engine):
        def _register_tools(self):
            super()._register_tools()

            async def future(_):
                dispatched.append(True)
                return {}

            for name in (FUTURE_TOOL, "documents_edit_cell"):
                if name not in self.tools:
                    self.register(name, "A newly registered private tool", Empty, future,
                                  read_only=True)

    monkeypatch.setattr(remote_setup, "Engine", UpdatedEngine)
    assert await remote_setup.setup_scopes("public-core") == PUBLIC_CORE_TOOLS
    assert FUTURE_TOOL not in PUBLIC_CORE_TOOLS
    assert not (set(PRIVATE_TOOLS) & PUBLIC_CORE_TOOLS)
    # Prove the fixture reaches the real dynamic catalogue used by legacy modes.
    assert FUTURE_TOOL in await remote_setup.setup_scopes("all")
    assert FUTURE_TOOL in await remote_setup.setup_scopes("files")
    assert "documents_edit_cell" in await remote_setup.setup_scopes("all")
    assert "documents_edit_cell" in await remote_setup.setup_scopes("files")
    assert dispatched == []


async def test_missing_public_tool_refuses_plan_without_narrowing_silently(monkeypatch):
    class IncompleteEngine(Engine):
        def _register_tools(self):
            super()._register_tools()
            self.tools.pop("files_read")

    monkeypatch.setattr(remote_setup, "Engine", IncompleteEngine)
    with pytest.raises(ValueError, match="complete public core"):
        await remote_setup.plan_remote_setup(resource=RESOURCE, mode="public-core")


@pytest.mark.parametrize("tool", PRIVATE_TOOLS)
def test_public_config_refuses_private_and_unknown_scopes(tool):
    with pytest.raises(ValueError, match="Public core requires"):
        public_config(scopes=SCOPES | {tool})


def test_public_config_refuses_subchat_even_without_subchat_scopes(tmp_path):
    with pytest.raises(ValueError, match="cannot select a Subchat"):
        public_config(subchat=selected_subchat(tmp_path))


async def test_review_save_reload_preserves_explicit_public_profile(tmp_path):
    # Both local setup entry points accept the same offering; no HTTP tool grants
    # are changed until the reviewed plan is explicitly confirmed in this fixture.
    draft = ConnectionSetupDraft(resource=RESOURCE, mode="public-core")
    plan = await remote_setup.plan_remote_setup(resource=draft.resource, mode=draft.mode)
    assert plan.scopes == PUBLIC_CORE_TOOLS and plan.tool_profile == "public-core"
    assert not tmp_path.joinpath("http-server").exists()
    controller = SetupController(tmp_path)
    reviewed = controller.review(plan)
    assert reviewed.phase == "review" and reviewed.configuration == plan
    result = await controller.confirm(reviewed.plan_id)
    assert result.phase == "configured" and result.configuration == plan
    assert load_http_config(tmp_path) == plan
    assert json.loads((tmp_path / "http-server/config.json").read_text())["tool_profile"] == (
        "public-core")


@pytest.mark.parametrize("forge", ["copy", "construct"])
@pytest.mark.parametrize("violation", ["private-scope", "subchat"])
async def test_unvalidated_models_are_rejected_before_save_or_review(tmp_path, forge, violation):
    plan = public_config()
    changes = ({"scopes": plan.scopes | {"codex_plugin_call"}} if violation == "private-scope"
               else {"subchat": selected_subchat(tmp_path)})
    forged = (plan.model_copy(update=changes) if forge == "copy" else
              HTTPServiceConfig.model_construct(**{**plan.model_dump(), **changes}))
    destination = tmp_path / "untouched-state"
    with pytest.raises(ValueError, match="Public core"):
        await save_http_config(destination, forged)
    assert not destination.exists()
    controller = SetupController(destination)
    with pytest.raises(ValueError, match="Public core"):
        controller.review(forged)
    assert controller.progress().phase == "new" and not destination.exists()


async def test_corrupt_public_configuration_cannot_start_service(tmp_path, monkeypatch):
    await save_http_config(tmp_path, public_config())
    path = tmp_path / "http-server/config.json"
    forged = load_http_config(tmp_path).model_copy(update={"scopes": SCOPES | {"devices_call"}})
    path.write_text(forged.model_dump_json())
    before = path.read_bytes()
    with pytest.raises(ValueError, match="missing or invalid"):
        load_http_config(tmp_path)
    monkeypatch.setattr(service_module, "OwnerCredentials",
                        lambda *a, **k: pytest.fail("Owner credentials must not be accessed"))
    with pytest.raises(ValueError, match="missing or invalid"):
        async with http_service(tmp_path):
            pytest.fail("Corrupt publication ceiling must not start")
    assert path.read_bytes() == before


@pytest.mark.parametrize("tools", [None, frozenset(), frozenset({"codex_plugin_call"}),
                                  frozenset({"devices_call"}), frozenset({FUTURE_TOOL})])
async def test_direct_backend_cannot_omit_or_expand_public_ceiling(tmp_path, tools):
    engine = Engine(tmp_path / "engine")
    store = AuthorizationStore(tmp_path / "auth", resource=RESOURCE, known_tools=frozenset())
    try:
        with pytest.raises(ValueError, match="Public core requires"):
            AuthorizedDeviceMCP(store, engine, owner="owner", device="fixture",
                                allowed_tools=tools, tool_profile="public-core")
        with pytest.raises(ValueError, match="cannot select a Subchat"):
            AuthorizedDeviceMCP(store, engine, owner="owner", device="fixture",
                                allowed_tools=SCOPES, tool_profile="public-core",
                                subchat_gateway=object())
    finally:
        store.close()
        await engine.close()


@pytest.mark.parametrize("tool", PRIVATE_TOOLS)
async def test_public_upgrade_refuses_private_tool_before_persistent_change(tmp_path, tool):
    config = await save_http_config(tmp_path, public_config())
    paths = [tmp_path / "http-server/config.json",
             tmp_path / "http-server/authorization/authorization.sqlite3"]
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    with pytest.raises(ValueError):
        await add_http_tools(tmp_path, frozenset({tool}))
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths] == before
    assert load_http_config(tmp_path) == config


async def test_public_upgrade_refuses_gateway_before_persistent_change(tmp_path):
    await save_http_config(tmp_path, public_config())
    path = tmp_path / "http-server/config.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Public core"):
        await add_http_tools(tmp_path, frozenset({"subchat_send"}),
                             subchat=selected_subchat(tmp_path))
    assert path.read_bytes() == before


@asynccontextmanager
async def selected_agent(tmp_path, monkeypatch, shared):
    if not shared:
        yield None
        return
    directory = tmp_path / "shared-agent"
    stopped = asyncio.Event()
    monkeypatch.setattr(connection, "local_credential", lambda *a, **k: "fixture-local-agent")
    task = asyncio.create_task(connection.serve(
        directory, credential="fixture-local-agent", shutdown=stopped))
    try:
        async with asyncio.timeout(10):
            while not (directory / "agent.json").exists():
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield directory
    finally:
        stopped.set()
        await asyncio.wait_for(task, 10)


@pytest.mark.parametrize("shared", [False, True])
async def test_real_http_public_catalog_and_call_keep_private_engine_unpublished(
    tmp_path, unused_tcp_port, monkeypatch, shared,
):
    dispatched = []

    class UpdatedEngine(Engine):
        def _register_tools(self):
            super()._register_tools()

            async def future(_):
                dispatched.append(True)
                return {}

            for name in (FUTURE_TOOL, "documents_edit_cell"):
                if name not in self.tools:
                    self.register(name, "A new private tool", Empty, future, read_only=True)

    monkeypatch.setattr(service_module, "Engine", UpdatedEngine)
    monkeypatch.setattr(connection, "Engine", UpdatedEngine)
    config = await setup(tmp_path / "http-state", unused_tcp_port, tool_profile="public-core")
    owner = OwnerCredentials(tmp_path / "http-state", resource=RESOURCE, owner="owner",
                             vault=MemoryVault())
    owner.initialize("synthetic owner password")
    async with selected_agent(tmp_path, monkeypatch, shared) as agent:
        async with http_service(tmp_path / "http-state", credentials=owner, agent_directory=agent):
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{config.port}", trust_env=False,
            ) as http:
                token = await authenticate(http)
                headers = await initialize(http, token)
                listed = await http.post("/mcp", headers=headers,
                    json={"jsonrpc": "2.0", "id": "catalog", "method": "tools/list"})
                assert {tool["name"] for tool in listed.json()["result"]["tools"]} == SCOPES
                for tool in PRIVATE_TOOLS:
                    denied = await http.post("/mcp", headers=headers, json={
                        "jsonrpc": "2.0", "id": tool, "method": "tools/call",
                        "params": {"name": tool, "arguments": {}},
                    })
                    assert denied.json()["error"]["code"] == -32602
                target = tmp_path / "fixture-file.txt"
                target.write_text("public fixture content")
                result = await http.post("/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": "read", "method": "tools/call",
                    "params": {"name": "files_read", "arguments": {"path": str(target)}},
                })
                receipt = result.json()["result"]["structuredContent"]
                assert receipt["state"] == "completed"
                assert receipt["data"]["text"] == "public fixture content"
    assert dispatched == []


async def test_public_upgrade_within_ceiling_keeps_existing_consent(tmp_path, unused_tcp_port):
    config = await setup(tmp_path, unused_tcp_port, tool_profile="public-core")
    store = AuthorizationStore(tmp_path / "http-server/authorization", resource=RESOURCE,
                               known_tools=config.scopes)
    try:
        code = store.approve(owner=config.owner, device=config.device, client=config.client,
            redirect=next(iter(config.redirects)), resource=RESOURCE, tools=config.scopes,
            challenge=pkce_s256("v" * 43))
        token = store.exchange_code(code=code, verifier="v" * 43, client=config.client,
            redirect=next(iter(config.redirects)), resource=RESOURCE).value
        before = store.db.execute("SELECT id,tools,expires,revoked FROM grants").fetchall()
    finally:
        store.close()
    result = await add_http_tools(tmp_path, frozenset({"browser_open"}))
    assert result["new_consent_required"] is True
    assert result["expanded_full_access_grants"] == 0
    expanded = load_http_config(tmp_path)
    assert expanded.tool_profile == "public-core" and expanded.scopes == SCOPES | {"browser_open"}
    store = AuthorizationStore(tmp_path / "http-server/authorization", resource=RESOURCE,
                               known_tools=expanded.scopes)
    engine = Engine(tmp_path / "standalone-engine")
    try:
        assert store.db.execute("SELECT id,tools,expires,revoked FROM grants").fetchall() == before
        backend = AuthorizedDeviceMCP(store, engine, owner=config.owner, device=config.device,
            client=config.client, allowed_tools=expanded.scopes, tool_profile="public-core")
        grant = await backend.authenticate(token)
        assert grant is not None
        assert {item["name"] for item in await backend.session(grant).catalog()} == config.scopes
    finally:
        store.close()
        await engine.close()


async def test_legacy_full_config_is_unchanged_and_can_publish_private_tools(tmp_path):
    config = public_config(tool_profile="full", scopes=SCOPES | {"codex_plugin_tools"})
    await save_http_config(tmp_path, config)
    raw = json.loads((tmp_path / "http-server/config.json").read_text())
    raw.pop("tool_profile")  # Existing beta52 files have no profile field.
    (tmp_path / "http-server/config.json").write_text(json.dumps(raw))
    assert load_http_config(tmp_path) == config
    await add_http_tools(tmp_path, frozenset({"devices_list"}))
    assert load_http_config(tmp_path).scopes == config.scopes | {"devices_list"}
    assert load_http_config(tmp_path).tool_profile == "full"


def test_cli_public_configuration_requires_explicit_profile_and_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "anywhere", "http-configure", "--state-dir", str(tmp_path), "--resource", RESOURCE,
        "--owner", "owner", "--client-id", "native", "--scope", "files_read",
        "--tool-profile", "public-core",
    ])
    cli.main()
    assert load_http_config(tmp_path).tool_profile == "public-core"
    assert load_http_config(tmp_path).scopes == frozenset({"files_read"})


@pytest.mark.parametrize("command", ["http-add-tools", "start", "login"])
def test_cli_cannot_switch_existing_profile(tmp_path, monkeypatch, command):
    destination = tmp_path / "untouched-state"
    monkeypatch.setattr(sys, "argv", [
        "anywhere", command, "--state-dir", str(destination), "--tool-profile", "full",
    ])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and not destination.exists()
