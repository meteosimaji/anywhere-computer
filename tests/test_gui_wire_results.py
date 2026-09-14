"""GUI provider failures and images must survive the public MCP result envelope."""

import json

import pytest
from mcp.types import CallToolResult, ImageContent
from test_gui_mcp import Peer
from test_plugin_image_results import PNG, image

from anywhere_computer.engine import Engine
from anywhere_computer.gui_mcp import GUIMCP
from anywhere_computer.mcp_server import MCPSession, _reply_result
from anywhere_computer.models import Reply, Request


@pytest.mark.parametrize("name", ["gui_observe", "gui_type", "gui_click", "gui_key"])
@pytest.mark.parametrize("provider_error", [False, True])
def test_gui_wire_projects_image_and_preserves_provider_error(name, provider_error):
    original = Reply(operation_id="a" * 32, state="completed", data={
        "content": [{"type": "text", "text": "日本語 observation"}, image()],
        "is_error": provider_error, "truncated": False,
        "provider_diagnostics": {"state": "dispatched_unverified", "retry_safe": False},
        "postcondition_verified": False,
    })
    before = original.model_dump_json()
    raw = _reply_result(name, original)
    result = CallToolResult.model_validate(raw)
    assert result.isError is provider_error
    assert isinstance(result.content[1], ImageContent)
    assert raw["content"][1:] == [image()]
    assert PNG not in raw["content"][0]["text"]
    assert PNG not in json.dumps(raw["structuredContent"])
    assert raw["structuredContent"]["data"]["provider_diagnostics"]["retry_safe"] is False
    assert raw["structuredContent"]["data"]["postcondition_verified"] is False
    assert original.model_dump_json() == before


async def test_failed_observation_reaches_mcp_client_and_remains_recoverable(tmp_path):
    engine = Engine(tmp_path)
    peer = Peer()
    calls = []

    async def incomplete(session_id, name, arguments, *, owner):
        calls.append(name)
        return {"content": [{"type": "text", "text": "AX tree incomplete"}], "isError": True}

    peer.call = incomplete
    engine.gui_mcp = GUIMCP(peer)

    async def catalog():
        return engine.catalog()

    async def execute(request):
        return await engine.execute(request, peer="owner")

    session = MCPSession(catalog, execute)
    session.initialized = session.ready = True
    try:
        response = await session.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "gui_observe", "arguments": {
                "session_id": "a" * 32, "app": "Editor", "window_id": 42,
                "request_id": "b" * 32,
            }},
        })
        result = response["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["state"] == "completed"
        assert result["structuredContent"]["data"]["is_error"] is True
        assert not engine.gui_mcp.observations
        recovered = await engine.execute(Request(operation_id="c" * 32,
            tool="operations_get", arguments={"operation_id": "b" * 32}), peer="owner")
        assert recovered.state == "completed"
        assert recovered.data["data"]["is_error"] is True
        assert calls == ["see"]
    finally:
        await engine.close()


@pytest.mark.parametrize("damage,reason", [
    ("truncated", "observation_truncated"),
    ("snapshots", "snapshot_reference_ambiguous"),
    ("elements", "element_reference_ambiguous"),
])
async def test_incomplete_observation_never_authorizes_input(damage, reason):
    from anywhere_computer.gui_mcp import GUIObserve, GUIType

    peer = Peer()
    gui = GUIMCP(peer)
    args = GUIObserve(session_id="a" * 32, app="Editor", window_id=42)
    earlier = await gui.observe(args, owner="owner")
    original = peer.call

    async def damaged(session_id, name, arguments, *, owner):
        raw = await original(session_id, name, arguments, owner=owner)
        suffix = {
            "truncated": "\n" + "x" * 70000,
            "snapshots": "\nSnapshot ID: different-snapshot",
            "elements": "\n  elem_1 - another control",
        }[damage]
        raw["content"][0]["text"] += suffix
        return raw

    peer.call = damaged
    result = await gui.observe(args, owner="owner")
    assert result["action_ready"] is False
    assert result["reason"] == reason
    assert "observation_id" not in result
    assert not gui.observations
    with pytest.raises(ValueError, match="missing"):
        await gui.act(GUIType(session_id="a" * 32,
            observation_id=earlier["observation_id"], text="must not type"), owner="owner")
    assert [name for name, _ in peer.calls] == ["see", "see"]
