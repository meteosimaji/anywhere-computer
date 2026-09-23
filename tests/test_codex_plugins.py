import copy
import os
from pathlib import Path

import pytest

from anywhere_computer import codex_plugins
from anywhere_computer.codex_plugins import (
    PluginCallOutcomeUnknown,
    call_codex_plugin_tool,
    list_codex_plugin_tools,
)


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Native executable fixture uses a POSIX shebang")
    script = tmp_path / "fake-codex"
    script.write_text(
        """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    p=json.loads(line); m=p.get('method')
    if 'id' not in p: continue
    if m == 'initialize': r={}
    elif m == 'thread/start':
        assert p['params']['ephemeral'] is True
        r={'thread':{'id':'thread-1','ephemeral':True}}
    elif m == 'mcpServerStatus/list':
        r={'servers':[{'name':'demo','tools':[{'name':'echo','description':'Echo',
            'inputSchema':{'type':'object'}}]}]}
    elif m == 'mcpServer/tool/call':
        r={'content':[{'type':'text','text':'ok'}], 'isError':False,
           'structuredContent':{'private':'discarded-by-size-check-only'}}
    elif m == 'thread/unsubscribe': r={}
    else: raise RuntimeError('unexpected method')
    print(json.dumps({'jsonrpc':'2.0','id':p['id'],'result':r}),flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(0o700)
    monkeypatch.setattr(codex_plugins, "_executable", lambda _: script)
    return script


async def test_catalog_is_ephemeral_and_exposes_schema_digest(fake_codex, tmp_path):
    result = await list_codex_plugin_tools(str(tmp_path), limit=30)
    assert result["servers"][0]["server"] == "demo"
    tool = result["servers"][0]["tools"][0]
    assert set(tool) == {
        "name", "description", "inputSchema", "annotations", "server", "catalog_sha256",
        "call_arguments",
    }
    assert len(tool["catalog_sha256"]) == 64


async def test_call_requires_fresh_catalog_digest_and_filters_result(fake_codex, tmp_path):
    catalog = await list_codex_plugin_tools(str(tmp_path))
    tool = catalog["servers"][0]["tools"][0]
    result = await call_codex_plugin_tool(
        str(tmp_path), "demo", "echo", {}, tool["catalog_sha256"],
    )
    assert result == {
        "content": [{"type": "text", "text": "ok"}],
        "is_error": False, "truncated": False,
        "structured_content": {"private": "discarded-by-size-check-only"},
    }
    with pytest.raises(ValueError, match="stale"):
        await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, "0" * 64)


async def test_recursive_server_and_tool_routes_are_rejected(fake_codex, tmp_path):
    with pytest.raises(ValueError):
        await call_codex_plugin_tool(str(tmp_path), "anywhere-computer", "echo", {}, "0" * 64)
    with pytest.raises(ValueError):
        await call_codex_plugin_tool(str(tmp_path), "demo", "devices_list", {}, "0" * 64)


def test_plugin_inputs_and_unknown_type_are_bounded():
    assert PluginCallOutcomeUnknown.__name__ == "PluginCallOutcomeUnknown"
    with pytest.raises(ValueError):
        codex_plugins._cwd("relative")
    with pytest.raises(ValueError):
        codex_plugins._cursor("x" * 2049)


@pytest.fixture
def stub_catalog(tmp_path, monkeypatch):
    rows = [{"name": "demo", "tools": {"echo": {
        "name": "echo", "description": "Echo", "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    }}}]
    state = {"rows": rows, "result": {"content": [{"type": "text", "text": "ok"}]},
             "calls": [], "next": None, "error": None}

    class Stub:
        def __init__(self, _):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def request(self, method, params):
            state["calls"].append((method, params))
            if method == "thread/start":
                assert params["ephemeral"] is True
                return {"thread": {"id": "isolated"}}
            if method == "mcpServerStatus/list":
                return {"data": state["rows"], "nextCursor": state["next"]}
            if method == "mcpServer/tool/call":
                if state["error"]:
                    raise state["error"]
                return state["result"]
            assert method == "thread/unsubscribe"
            return {}

    monkeypatch.setattr(codex_plugins, "_Session", Stub)
    monkeypatch.setattr(codex_plugins, "_executable", lambda _: Path("/fixture"))
    return state


async def test_catalog_skips_self_and_returns_single_page(stub_catalog, tmp_path):
    stub_catalog["rows"].append({"name": "plugin_anywhere-computer", "tools": {}})
    stub_catalog["next"] = "page-two"
    result = await list_codex_plugin_tools(str(tmp_path))
    assert [row["server"] for row in result["servers"]] == ["demo"]
    assert result["next_cursor"] == "page-two"
    assert sum(m == "mcpServerStatus/list" for m, _ in stub_catalog["calls"]) == 1


async def test_duplicate_and_changed_annotation_prevent_dispatch(stub_catalog, tmp_path):
    result = await list_codex_plugin_tools(str(tmp_path))
    digest = result["servers"][0]["tools"][0]["catalog_sha256"]
    stub_catalog["rows"][0]["tools"]["echo"]["annotations"]["readOnlyHint"] = False
    with pytest.raises(ValueError, match="stale"):
        await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, digest)
    assert not any(m == "mcpServer/tool/call" for m, _ in stub_catalog["calls"])
    stub_catalog["rows"].append(copy.deepcopy(stub_catalog["rows"][0]))
    with pytest.raises(codex_plugins.PluginPreflightError, match="plugin_catalog_failed") as caught:
        await list_codex_plugin_tools(str(tmp_path))
    assert caught.value.details["failure_kind"] == "invalid_response"
    assert "duplicate" in str(caught.value.__cause__)


@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionError(), RuntimeError()])
async def test_dispatched_error_is_unknown(stub_catalog, tmp_path, failure):
    result = await list_codex_plugin_tools(str(tmp_path))
    digest = result["servers"][0]["tools"][0]["catalog_sha256"]
    stub_catalog["error"] = failure
    with pytest.raises(PluginCallOutcomeUnknown):
        await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, digest)


@pytest.mark.parametrize("result", [
    {"content": [], "isError": "false"}, {"content": "malformed"},
    {"content": [{"type": "text", "text": 5}]}, {"structuredContent": []},
])
async def test_malformed_result_is_unknown(stub_catalog, tmp_path, result):
    catalog = await list_codex_plugin_tools(str(tmp_path))
    digest = catalog["servers"][0]["tools"][0]["catalog_sha256"]
    stub_catalog["result"] = result
    with pytest.raises(PluginCallOutcomeUnknown):
        await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, digest)


def test_result_utf8_bounds_and_metadata_filtering():
    result = codex_plugins._tool_result({
        "content": [{"type": "audio", "data": "discard"},
                    {"type": "text", "text": "あ" * 20000},
                    {"type": "text", "text": "い" * 20000}],
        "structuredContent": {"value": 1, "_meta": {"secret": "excluded"}},
        "_meta": {"secret": "excluded"},
    })
    assert result["truncated"] is True
    assert result["unsupported_content_items"] == 1
    assert sum(len(row["text"].encode()) for row in result["content"]) <= 65536
    assert result["structured_content"] == {"value": 1}
    assert "excluded" not in str(result)


async def test_transport_disallows_model_and_persistent_threads():
    session = codex_plugins._Session(Path("/not-started"))
    for method, params in [("turn/start", {}), ("thread/resume", {}),
                           ("thread/start", {"ephemeral": False})]:
        with pytest.raises(ValueError):
            await session.request(method, params)


async def test_targeted_inspection_and_summary_preserve_digest(stub_catalog, tmp_path):
    row = stub_catalog['rows'][0]
    row['runtimeStatus'] = 'connected'
    row['authStatus'] = 'unsupported'
    summary = await list_codex_plugin_tools(str(tmp_path), query='echo', summary=True)
    server = summary['servers'][0]
    assert server['tools'] == [] and server['tool_count'] == 1
    assert server['availability'] == 'ready_to_call'
    assert server['execution_verified'] is False
    inspected = await list_codex_plugin_tools(**server['inspect_arguments'], tool='echo')
    descriptor = inspected['servers'][0]['tools'][0]
    result = await call_codex_plugin_tool(
        str(tmp_path), 'demo', 'echo', {}, descriptor['catalog_sha256'],
    )
    assert result['is_error'] is False
    assert not any(method == 'turn/start' for method, _ in stub_catalog['calls'])


@pytest.mark.parametrize('runtime,auth,expected', [
    ('authenticationRequired', 'unknown', 'authentication_required'),
    ('connected', 'notLoggedIn', 'authentication_required'),
    ('starting', 'unknown', 'runtime_not_ready'),
    ('disabled', 'unknown', 'unavailable'),
])
async def test_unavailable_server_never_dispatches(
    stub_catalog, tmp_path, runtime, auth, expected, monkeypatch,
):
    monkeypatch.setattr(codex_plugins, "STARTUP_TIMEOUT", 0.02)
    stub_catalog['rows'][0].update(runtimeStatus=runtime, authStatus=auth)
    inspected = await list_codex_plugin_tools(str(tmp_path), server='demo', tool='echo')
    assert inspected['servers'][0]['availability'] == expected
    descriptor = inspected['servers'][0]['tools'][0]
    with pytest.raises(codex_plugins.PluginPreflightError) as caught:
        await call_codex_plugin_tool(
            str(tmp_path), 'demo', 'echo', {}, descriptor['catalog_sha256'],
        )
    assert caught.value.code == expected
    assert not any(method == 'mcpServer/tool/call' for method, _ in stub_catalog['calls'])


async def test_absent_tool_and_stale_catalog_have_distinct_codes(
    stub_catalog, tmp_path, monkeypatch,
):
    monkeypatch.setattr(codex_plugins, "STARTUP_TIMEOUT", 0.1)
    for tool, expected in [('missing', 'tool_not_found'), ('echo', 'catalog_stale')]:
        with pytest.raises(codex_plugins.PluginPreflightError) as caught:
            await call_codex_plugin_tool(str(tmp_path), 'demo', tool, {}, '0' * 64)
        assert caught.value.code == expected
    assert not any(method == 'mcpServer/tool/call' for method, _ in stub_catalog['calls'])


@pytest.mark.parametrize("tool", ["Devices_list", "CODEX_PLUGIN_x", "MCP__CODEX_APP__x"])
async def test_mixed_case_local_routes_are_rejected(fake_codex, tmp_path, tool):
    with pytest.raises(ValueError, match="forbidden"):
        await call_codex_plugin_tool(str(tmp_path), "demo", tool, {}, "0" * 64)


async def test_incomplete_scan_is_not_tool_absence(stub_catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(codex_plugins, "MAX_PAGES", 1)
    stub_catalog["next"] = "next-page"
    with pytest.raises(codex_plugins.PluginPreflightError) as caught:
        await call_codex_plugin_tool(str(tmp_path), "later-server", "echo", {}, "0" * 64)
    assert caught.value.code == "catalog_incomplete"
    assert not any(method == "mcpServer/tool/call" for method, _ in stub_catalog["calls"])


async def test_preflight_failure_unsubscribes_without_dispatch(stub_catalog, tmp_path):
    with pytest.raises(codex_plugins.PluginPreflightError):
        await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, "0" * 64)
    methods = [method for method, _ in stub_catalog["calls"]]
    assert methods[-1] == "thread/unsubscribe"
    assert "mcpServer/tool/call" not in methods


async def test_inspection_supplies_exact_call_arguments(stub_catalog, tmp_path):
    result = await list_codex_plugin_tools(str(tmp_path), server="demo", tool="echo")
    descriptor = result["servers"][0]["tools"][0]
    arguments = descriptor["call_arguments"]
    assert arguments == {
        "cwd": str(tmp_path.resolve()), "server": "demo", "tool": "echo",
        "catalog_sha256": descriptor["catalog_sha256"],
    }
    assert (await call_codex_plugin_tool(**arguments, arguments={}))["is_error"] is False


async def test_stale_diagnostic_identifies_both_digests(stub_catalog, tmp_path):
    catalog = await list_codex_plugin_tools(str(tmp_path))
    actual = catalog["servers"][0]["tools"][0]["catalog_sha256"]
    with pytest.raises(codex_plugins.PluginPreflightError) as caught:
        await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, "0" * 64)
    assert caught.value.details == {
        "cwd": str(tmp_path.resolve()), "server": "demo", "tool": "echo",
        "received_catalog_sha256": "0" * 64, "current_catalog_sha256": actual,
    }
    assert not any(method == "mcpServer/tool/call" for method, _ in stub_catalog["calls"])


async def test_stale_details_survive_ledger_replay(stub_catalog, tmp_path):
    from anywhere_computer.engine import Engine
    from anywhere_computer.models import Request

    engine = Engine(tmp_path / "engine")
    request = Request(operation_id="a" * 32, tool="codex_plugin_call", arguments={
        "cwd": str(tmp_path), "server": "demo", "tool": "echo",
        "arguments": {}, "catalog_sha256": "0" * 64,
    })
    try:
        failed = await engine.execute(request)
        count = len(stub_catalog["calls"])
        replay = await engine.execute(request)
        assert failed == replay == engine.ledger.get(request.operation_id)
        assert failed.state == "failed"
        assert failed.data["dispatched"] is False
        assert failed.data["details"]["received_catalog_sha256"] == "0" * 64
        assert len(failed.data["details"]["current_catalog_sha256"]) == 64
        assert len(stub_catalog["calls"]) == count
    finally:
        await engine.close()


async def test_workspace_mismatch_keeps_fail_closed_behavior(stub_catalog, tmp_path):
    catalog = await list_codex_plugin_tools(str(tmp_path))
    digest = catalog["servers"][0]["tools"][0]["catalog_sha256"]
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(codex_plugins.PluginPreflightError) as caught:
        await call_codex_plugin_tool(str(other), "demo", "echo", {}, digest)
    assert caught.value.code == "catalog_stale"
    assert caught.value.details["cwd"] == str(other.resolve())
    assert not any(method == "mcpServer/tool/call" for method, _ in stub_catalog["calls"])


async def test_cleanup_failure_does_not_mask_preflight(stub_catalog, tmp_path, monkeypatch):
    original = codex_plugins._Session.request

    async def request(self, method, params):
        if method == "thread/unsubscribe":
            raise ConnectionError("cleanup failed")
        return await original(self, method, params)

    monkeypatch.setattr(codex_plugins._Session, "request", request)
    with pytest.raises(codex_plugins.PluginPreflightError, match="catalog_stale"):
        await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, "0" * 64)


@pytest.mark.parametrize("case", ["limit", "cursor", "selection", "digest", "recursive"])
async def test_invalid_requests_do_not_start_a_runtime(tmp_path, monkeypatch, case):
    def forbidden(_):
        pytest.fail("Invalid input must be rejected before launching Codex")

    monkeypatch.setattr(codex_plugins, "_executable", forbidden)
    with pytest.raises(ValueError):
        if case == "limit":
            await list_codex_plugin_tools(str(tmp_path), limit=0)
        elif case == "cursor":
            await list_codex_plugin_tools(str(tmp_path), cursor="")
        elif case == "selection":
            await list_codex_plugin_tools(str(tmp_path), tool="echo")
        elif case == "digest":
            await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, "bad")
        else:
            await list_codex_plugin_tools(str(tmp_path), server="anywhere-computer")


@pytest.mark.parametrize("name", [
    "anywhere_computer.codex_plugin_call", "Anywhere_Computer.files_read",
    "anywhere-computer.devices_call", "mcp__anywhere_computer__files_read",
])
async def test_namespaced_self_tools_are_hidden_and_never_dispatched(stub_catalog, tmp_path, name):
    stub_catalog["rows"][0]["name"] = "codex_apps"
    stub_catalog["rows"][0]["tools"][name] = {
        "description": "self", "inputSchema": {"type": "object"},
    }
    catalog = await list_codex_plugin_tools(str(tmp_path))
    assert [t["name"] for t in catalog["servers"][0]["tools"]] == ["echo"]
    stub_catalog["calls"].clear()
    with pytest.raises(ValueError):
        await call_codex_plugin_tool(str(tmp_path), "codex_apps", name, {}, "0" * 64)
    assert stub_catalog["calls"] == []
    with pytest.raises(ValueError):
        await list_codex_plugin_tools(str(tmp_path), server="codex_apps", tool=name)
    assert stub_catalog["calls"] == []


@pytest.mark.parametrize("name", [
    "google_calendar.get_colors", "other.anywhere_computer", "other_anywhere_computer.echo",
])
async def test_other_plugin_namespace_remains_callable(stub_catalog, tmp_path, name):
    stub_catalog["rows"][0]["tools"] = {
        name: {"description": "Colors", "inputSchema": {}},
    }
    catalog = await list_codex_plugin_tools(str(tmp_path))
    descriptor = catalog["servers"][0]["tools"][0]
    assert (await call_codex_plugin_tool(**descriptor["call_arguments"], arguments={}))[
        "is_error"
    ] is False


async def test_full_description_detail_tail_search_and_digest(stub_catalog, tmp_path):
    description = "使い方" * 700 + " tail-only-search-word"
    stub_catalog["rows"][0]["tools"]["echo"]["description"] = description
    detail = await list_codex_plugin_tools(str(tmp_path), server="demo", tool="echo")
    descriptor = detail["servers"][0]["tools"][0]
    assert descriptor["description"] == description
    assert not descriptor.get("description_truncated", False)
    summary = await list_codex_plugin_tools(
        str(tmp_path), query="tail-only-search-word", summary=True,
    )
    assert summary["servers"][0]["tool_count"] == 1
    broad = await list_codex_plugin_tools(str(tmp_path))
    preview = broad["servers"][0]["tools"][0]
    assert preview["description_truncated"] is True
    assert len(preview["description"]) == 1000
    assert preview["catalog_sha256"] == descriptor["catalog_sha256"]
    stub_catalog["rows"][0]["tools"]["echo"]["description"] += " changed"
    with pytest.raises(codex_plugins.PluginPreflightError, match="catalog_stale"):
        await call_codex_plugin_tool(**descriptor["call_arguments"], arguments={})


@pytest.mark.parametrize("server,tool", [
    ("cua_repl", "js"), ("codex_apps", "cua_repl.js"),
    ("unified-computer-use", "js"), ("codex_apps", "mcp__cua_repl__js"),
])
async def test_computer_use_reports_context_and_blocks_before_runtime(
    stub_catalog, tmp_path, server, tool,
):
    stub_catalog["rows"] = [{"name": server, "runtimeStatus": "connected", "tools": {
        tool: {"description": "Computer Use", "inputSchema": {}},
    }}]
    catalog = await list_codex_plugin_tools(str(tmp_path), server=server, tool=tool)
    row = catalog["servers"][0]
    assert row["availability"] == "ready_to_call"
    descriptor = row["tools"][0]
    assert descriptor["compatibility"]["screen_read"] == "unverified"
    assert descriptor["compatibility"]["native_actions"] == "unsupported_execution_context"
    assert descriptor["compatibility"]["browser_actions"] == "unsupported_execution_context"
    summary = await list_codex_plugin_tools(str(tmp_path), server=server, summary=True)
    assert summary["servers"][0]["computer_use_compatibility"] == descriptor["compatibility"]
    stub_catalog["calls"].clear()
    with pytest.raises(codex_plugins.PluginPreflightError) as caught:
        await call_codex_plugin_tool(**descriptor["call_arguments"], arguments={"code": "1+1"})
    assert caught.value.code == "unsupported_execution_context"
    assert stub_catalog["calls"] == []


@pytest.mark.parametrize("method,stage", [
    ("initialize", "startup"), ("mcpServerStatus/list", "catalog"),
    ("mcpServer/tool/call", "after_dispatch"),
])
async def test_rpc_diagnostics_are_redacted_and_recoverable(fake_codex, tmp_path, method, stage):
    from anywhere_computer.engine import Engine
    from anywhere_computer.models import Request

    # A real subprocess writes more than pipe capacity, then rejects one request.
    # No newline is needed for stderr draining and no provider text is persisted.
    catalog = await list_codex_plugin_tools(str(tmp_path), server="demo", tool="echo")
    args = catalog["servers"][0]["tools"][0]["call_arguments"]
    source = fake_codex.read_text()
    source = source.replace(
        "    if m == 'initialize': r={}",
        f"""    if m == {method!r}:
        sys.stderr.write('fixture-private-secret' * 20000)
        sys.stderr.flush()
        print(json.dumps({{'id':p['id'], 'error':{{'code':-32602,
            'message':'Invalid params fixture-private-secret',
            'data':{{'token':'fixture-private-secret'}}}}}}), flush=True)
        continue
    if m == 'initialize': r={{}}""",
    )
    fake_codex.write_text(source)
    engine = Engine(tmp_path / "engine")
    request = Request(operation_id="b" * 32, tool="codex_plugin_call",
                      arguments={**args, "arguments": {}})
    try:
        reply = await engine.execute(request)
        assert reply.state == ("unknown" if stage == "after_dispatch" else "failed")
        assert reply.data["failure_stage"] == stage
        details = reply.data["details"]
        assert details["rpc_code"] == -32602
        assert details["message_kind"] == "invalid_params"
        assert details["data_present"] is True
        diagnostic = details["runtime_diagnostics"]
        assert diagnostic["stderr_bytes_seen"] >= 300000
        assert len(diagnostic["stderr_events"]) <= 32
        assert diagnostic["events_dropped"] is True
        assert "fixture-private-secret" not in reply.model_dump_json()
        assert await engine.execute(request) == reply
        recovered = await engine.execute(Request(
            operation_id="c" * 32, tool="operations_get",
            arguments={"operation_id": request.operation_id},
        ))
        assert recovered.data["data"] == reply.data
    finally:
        await engine.close()


async def test_runtime_launch_failure_is_classified_without_raw_exception(tmp_path, monkeypatch):
    def fail(_):
        raise FileNotFoundError("private-path-or-secret")

    monkeypatch.setattr(codex_plugins, "_executable", fail)
    with pytest.raises(codex_plugins.PluginPreflightError) as caught:
        await list_codex_plugin_tools(str(tmp_path))
    assert caught.value.details["failure_stage"] == "startup"
    assert "private-path-or-secret" not in str(caught.value)


async def test_oversized_catalog_fails_instead_of_returning_partial_definition(
    stub_catalog, tmp_path,
):
    stub_catalog["rows"][0]["tools"]["echo"]["description"] = "x" * (2 * 1024 * 1024)
    with pytest.raises(codex_plugins.PluginPreflightError, match="plugin_catalog_failed") as caught:
        await list_codex_plugin_tools(str(tmp_path), server="demo", tool="echo")
    assert caught.value.details["failure_stage"] == "catalog"
    assert not any(m == "mcpServer/tool/call" for m, _ in stub_catalog["calls"])


async def test_over_thousand_catalog_is_visible_and_exact_selection_works(stub_catalog, tmp_path):
    stub_catalog["rows"][0]["tools"] = {
        f"tool{i}": {"description": "Small", "inputSchema": {}} for i in range(1001)
    }
    broad = await list_codex_plugin_tools(str(tmp_path), summary=True)
    row = broad["servers"][0]
    assert row["catalog_complete"] is False
    assert row["omitted_tool_count"] == 1
    detail = await list_codex_plugin_tools(str(tmp_path), server="demo", tool="tool1000")
    descriptor = detail["servers"][0]["tools"][0]
    assert descriptor["name"] == "tool1000"
    assert (await call_codex_plugin_tool(**descriptor["call_arguments"], arguments={}))[
        "is_error"
    ] is False


async def test_unrelated_large_catalog_does_not_block_exact_tool(stub_catalog, tmp_path):
    stub_catalog["rows"].append({"name": "large", "tools": {
        "huge": {"description": "x" * (3 * 1024 * 1024), "inputSchema": {}},
    }})
    result = await list_codex_plugin_tools(str(tmp_path), server="demo", tool="echo")
    assert result["servers"][0]["tools"][0]["name"] == "echo"


@pytest.mark.parametrize("ready_state,expected", [
    ("connected", None),
    ("authenticationRequired", "authentication_required"),
    ("changed", "catalog_stale"),
])
async def test_call_waits_for_same_runtime_startup(
    stub_catalog, tmp_path, monkeypatch, ready_state, expected,
):
    catalog = await list_codex_plugin_tools(str(tmp_path))
    digest = catalog["servers"][0]["tools"][0]["catalog_sha256"]
    original = codex_plugins._Session.request
    polls = []

    async def starting_then_ready(self, method, params):
        if method == "mcpServerStatus/list":
            polls.append(params["threadId"])
            stub_catalog["rows"][0]["runtimeStatus"] = (
                "starting" if len(polls) == 1 else
                "connected" if ready_state == "changed" else ready_state
            )
            if len(polls) > 1 and ready_state == "changed":
                stub_catalog["rows"][0]["tools"]["echo"]["description"] = "Changed"
        return await original(self, method, params)

    monkeypatch.setattr(codex_plugins._Session, "request", starting_then_ready)
    stub_catalog["calls"].clear()
    if expected:
        with pytest.raises(codex_plugins.PluginPreflightError) as caught:
            await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, digest)
        assert caught.value.code == expected
    else:
        result = await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, digest)
        assert result["content"][0]["text"] == "ok"
    assert polls == ["isolated", "isolated"]
    methods = [method for method, _ in stub_catalog["calls"]]
    assert methods.count("thread/start") == 1
    assert methods.count("mcpServer/tool/call") == (0 if expected else 1)


@pytest.mark.parametrize("partial_catalog", [False, True])
async def test_call_waits_for_connected_server_to_publish_tools(
    stub_catalog, tmp_path, monkeypatch, partial_catalog,
):
    catalog = await list_codex_plugin_tools(str(tmp_path))
    digest = catalog["servers"][0]["tools"][0]["catalog_sha256"]
    original = codex_plugins._Session.request
    polls = []

    async def empty_then_published(self, method, params):
        if method == "mcpServerStatus/list":
            polls.append(params["threadId"])
            stub_catalog["rows"][0]["runtimeStatus"] = "connected"
            if len(polls) == 1:
                stub_catalog["rows"][0]["tools"] = ({"other": {
                    "name": "other", "description": "Other",
                    "inputSchema": {"type": "object"},
                }} if partial_catalog else {})
        result = copy.deepcopy(await original(self, method, params))
        if method == "mcpServerStatus/list" and len(polls) == 1:
            stub_catalog["rows"][0]["tools"] = {"echo": {
                "name": "echo", "description": "Echo",
                "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": True},
            }}
        return result

    monkeypatch.setattr(codex_plugins._Session, "request", empty_then_published)
    result = await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, digest)
    assert result["content"][0]["text"] == "ok"
    assert polls == ["isolated", "isolated"]
    assert sum(method == "mcpServer/tool/call" for method, _ in stub_catalog["calls"]) == 1


async def test_connected_server_without_published_tools_is_not_tool_absence(
    stub_catalog, tmp_path, monkeypatch,
):
    monkeypatch.setattr(codex_plugins, "STARTUP_TIMEOUT", 0.02)
    stub_catalog["rows"][0].update(runtimeStatus="connected", tools={})
    with pytest.raises(codex_plugins.PluginPreflightError) as caught:
        await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, "0" * 64)
    assert caught.value.code == "runtime_not_ready"
    assert not any(method == "mcpServer/tool/call" for method, _ in stub_catalog["calls"])


async def test_connected_empty_catalog_does_not_mask_authentication_failure(
    stub_catalog, tmp_path, monkeypatch,
):
    monkeypatch.setattr(codex_plugins, "STARTUP_TIMEOUT", 0.1)
    stub_catalog["rows"][0].update(
        runtimeStatus="connected", authStatus="notLoggedIn", tools={},
    )
    with pytest.raises(codex_plugins.PluginPreflightError) as caught:
        await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, "0" * 64)
    assert caught.value.code == "authentication_required"
    assert not any(method == "mcpServer/tool/call" for method, _ in stub_catalog["calls"])


async def test_exact_tool_inspection_waits_for_same_runtime_startup(
    stub_catalog, tmp_path, monkeypatch,
):
    original = codex_plugins._Session.request
    polls = []

    async def starting_then_ready(self, method, params):
        if method == 'mcpServerStatus/list':
            polls.append(params['threadId'])
            stub_catalog['rows'][0]['runtimeStatus'] = (
                'starting' if len(polls) == 1 else 'connected')
            if len(polls) == 1:
                stub_catalog['rows'][0]['tools'] = {}
            else:
                stub_catalog['rows'][0]['tools'] = {'echo': {
                    'name': 'echo', 'description': 'Echo',
                    'inputSchema': {'type': 'object'},
                }}
        return await original(self, method, params)

    monkeypatch.setattr(codex_plugins._Session, 'request', starting_then_ready)
    result = await list_codex_plugin_tools(str(tmp_path), server='demo', tool='echo')
    row = result['servers'][0]
    assert row['availability'] == 'ready_to_call'
    assert [tool['name'] for tool in row['tools']] == ['echo']
    assert polls == ['isolated', 'isolated']
    assert sum(method == 'thread/start' for method, _ in stub_catalog['calls']) == 1


async def test_exact_tool_inspection_waits_for_connected_catalog(
    stub_catalog, tmp_path, monkeypatch,
):
    original = codex_plugins._Session.request
    polls = []

    async def empty_then_published(self, method, params):
        if method == 'mcpServerStatus/list':
            polls.append(params['threadId'])
            stub_catalog['rows'][0]['runtimeStatus'] = 'connected'
            stub_catalog['rows'][0]['tools'] = {} if len(polls) == 1 else {'echo': {
                'name': 'echo', 'description': 'Echo',
                'inputSchema': {'type': 'object'},
            }}
        return await original(self, method, params)

    monkeypatch.setattr(codex_plugins._Session, 'request', empty_then_published)
    result = await list_codex_plugin_tools(str(tmp_path), server='demo', tool='echo')
    assert result['servers'][0]['availability'] == 'ready_to_call'
    assert [tool['name'] for tool in result['servers'][0]['tools']] == ['echo']
    assert polls == ['isolated', 'isolated']


async def test_startup_catalog_transport_failure_is_not_retried(
    stub_catalog, tmp_path, monkeypatch,
):
    catalog = await list_codex_plugin_tools(str(tmp_path))
    digest = catalog["servers"][0]["tools"][0]["catalog_sha256"]
    original = codex_plugins._Session.request
    polls = 0

    async def failing_startup(self, method, params):
        nonlocal polls
        if method == "mcpServerStatus/list":
            polls += 1
            if polls == 2:
                raise ConnectionError("startup transport lost")
            stub_catalog["rows"][0]["runtimeStatus"] = "starting"
        return await original(self, method, params)

    monkeypatch.setattr(codex_plugins._Session, "request", failing_startup)
    stub_catalog["calls"].clear()
    with pytest.raises(codex_plugins.PluginPreflightError, match="plugin_catalog_failed"):
        await call_codex_plugin_tool(str(tmp_path), "demo", "echo", {}, digest)
    assert polls == 2
    assert not any(method == "mcpServer/tool/call" for method, _ in stub_catalog["calls"])
