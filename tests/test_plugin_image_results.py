"""Image results must reach MCP clients without base64 in the text envelope."""

import asyncio
import base64
import hashlib
import json

import pytest

from anywhere_computer.codex_plugins import _tool_result
from anywhere_computer.mcp_server import MCPSession
from anywhere_computer.models import Reply

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
       "/x8AAwMCAO+a5XcAAAAASUVORK5CYII=")


def image():
    return {"type": "image", "mimeType": "image/png", "data": PNG}


@pytest.mark.parametrize("is_error", [False, True])
def test_recovered_image_is_native_without_losing_original_error(is_error):
    from anywhere_computer.mcp_server import _reply_result

    original = Reply(operation_id="a" * 32, state="completed",
                     data=_tool_result({"content": [image()], "isError": is_error}))
    recovered = Reply(operation_id="b" * 32, state="completed",
                      data=original.model_dump(mode="json"))
    result = _reply_result("operations_get", recovered)
    assert result["content"][1:] == [image()]
    assert PNG not in result["content"][0]["text"]
    assert PNG not in json.dumps(result["structuredContent"])
    assert result["isError"] is False  # Recovery succeeded; inner result retains failure.
    assert result["structuredContent"]["data"]["data"]["is_error"] is is_error
    assert original.data["content"] == [image()]


async def test_delayed_image_survives_new_mcp_session(tmp_path, monkeypatch):
    from anywhere_computer import codex_plugins
    from anywhere_computer import engine as engine_module
    from anywhere_computer.engine import Engine

    engine = Engine(tmp_path)
    release = asyncio.Event()

    async def delayed(**_):
        await release.wait()
        return _tool_result({"content": [image()]})

    async def catalog():
        return engine.catalog()

    monkeypatch.setattr(codex_plugins, "call_codex_plugin_tool", delayed)
    first = MCPSession(catalog, engine.execute)
    first.initialized = first.ready = True
    monkeypatch.setattr(engine_module, "OBSERVER_WAIT_SECONDS", 0.01)
    identity = "f" * 32
    try:
        reply = await first.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "codex_plugin_call", "arguments": {
                "request_id": identity, "cwd": str(tmp_path), "server": "fixture",
                "tool": "image", "catalog_sha256": "a" * 64,
            }}})
        assert reply["result"]["structuredContent"]["state"] == "running"
        release.set()
        await asyncio.gather(*engine.inflight.values())
        monkeypatch.setattr(engine_module, "OBSERVER_WAIT_SECONDS", 5.0)
        second = MCPSession(catalog, engine.execute)
        second.initialized = second.ready = True
        recovered = await second.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "operations_get", "arguments": {"operation_id": identity}}})
        result = recovered["result"]
        assert result["content"][1:] == [image()]
        assert PNG not in result["content"][0]["text"]
        assert result["structuredContent"]["data"]["operation_id"] == identity
        assert engine.ledger.get(identity).data["content"] == [image()]
    finally:
        release.set()
        await engine.close()


def test_plugin_result_preserves_image_with_text():
    result = _tool_result({"content": [{"type": "text", "text": "screenshot"}, image()]})
    assert result["content"] == [{"type": "text", "text": "screenshot"}, image()]
    assert result["truncated"] is False


def test_exhausted_text_budget_does_not_hide_later_screenshot():
    result = _tool_result({"content": [
        {"type": "text", "text": "a" * 70000}, image(),
    ]})
    assert result["content"][-1] == image()
    assert result["truncated"] is True


@pytest.mark.parametrize("is_error", [False, True])
async def test_native_image_projection_preserves_ledger_and_error(is_error):
    data = {"content": [image()], "is_error": is_error, "truncated": False}
    reply = Reply(operation_id="a" * 32, state="completed", data=data)

    async def catalog():
        return [{"name": "codex_plugin_call"}]

    async def execute(request):
        return reply

    session = MCPSession(catalog, execute)
    session.initialized = session.ready = True
    response = await session.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "codex_plugin_call"},
    })
    result = response["result"]
    assert result["content"][1:] == [image()]
    assert PNG not in result["content"][0]["text"]
    assert PNG not in json.dumps(result["structuredContent"])
    assert result["structuredContent"]["operation_id"] == reply.operation_id
    assert result["isError"] is is_error
    assert reply.data["content"] == [image()]


@pytest.mark.parametrize("item", [
    {"type": "image", "mimeType": "image/png", "data": "%invalid%"},
    {"type": "image", "mimeType": "image/png", "data": ""},
    {"type": "image", "mimeType": "image/jpeg", "data": PNG},
    {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="},
    {"type": "image", "mimeType": "image/png", "data": PNG + "\n"},
    {"type": "image", "mimeType": "image/png", "data": None},
    {"type": "image", "data": PNG},
])
def test_malformed_image_results_are_rejected(item):
    with pytest.raises(ValueError):
        _tool_result({"content": [item]})


def test_unsupported_image_type_is_explicitly_omitted():
    result = _tool_result({"content": [
        {"type": "image", "mimeType": "image/svg+xml", "data": "PHN2Zy8+"},
        {"type": "text", "text": "still readable"},
    ]})
    assert result["content"] == [{"type": "text", "text": "still readable"}]
    assert result["omitted_image_items"] == 1
    assert result["truncated"] is True


def test_image_metadata_is_not_forwarded():
    item = {**image(), "_meta": {"private": "excluded"}, "annotations": {"priority": 1}}
    result = _tool_result({"content": [item], "_meta": {"private": "excluded"}})
    assert result["content"] == [image()]
    assert "excluded" not in json.dumps(result)


def test_image_count_is_bounded():
    result = _tool_result({"content": [image() for _ in range(6)]})
    assert result["content"] == [image() for _ in range(4)]
    assert result["omitted_image_items"] == 2
    assert result["truncated"] is True


def test_image_byte_budget_is_shared(monkeypatch):
    from anywhere_computer import mcp_results

    size = len(base64.b64decode(PNG))
    monkeypatch.setattr(mcp_results, "IMAGE_LIMIT", size * 2)
    result = _tool_result({"content": [image(), image(), image()]})
    assert result["content"] == [image(), image()]
    assert result["omitted_image_items"] == 1


def test_oversized_image_does_not_hide_subsequent_text(monkeypatch):
    from anywhere_computer import mcp_results

    monkeypatch.setattr(mcp_results, "IMAGE_LIMIT", 1)
    result = _tool_result({"content": [image(), {"type": "text", "text": "done"}]})
    assert result["content"] == [{"type": "text", "text": "done"}]
    assert result["omitted_image_items"] == 1


@pytest.mark.parametrize('name', ['codex_plugin_call', 'mcp_call'])
def test_native_image_result_matches_official_sdk_schema(name):
    from mcp.types import CallToolResult, ImageContent

    from anywhere_computer.mcp_server import _reply_result

    reply = Reply(operation_id="b" * 32, state="completed", data=_tool_result({
        "content": [image()],
    }))
    result = CallToolResult.model_validate(_reply_result(name, reply))
    assert isinstance(result.content[1], ImageContent)
    summary = result.structuredContent["data"]["content"][0]
    assert summary["bytes"] == len(base64.b64decode(PNG))
    assert summary["sha256"] == hashlib.sha256(base64.b64decode(PNG)).hexdigest()


@pytest.mark.parametrize("name,state", [
    ("files_read", "completed"), ("codex_plugin_tools", "completed"),
    ("codex_plugin_call", "failed"), ("codex_plugin_call", "unknown"),
])
def test_only_completed_plugin_results_are_projected(name, state):
    from anywhere_computer.mcp_server import _reply_result

    reply = Reply(operation_id="c" * 32, state=state, data={"content": [image()]})
    result = _reply_result(name, reply)
    assert len(result["content"]) == 1
    assert result["structuredContent"] == reply.model_dump(mode="json")


def test_largest_allowed_image_fits_transport():
    from anywhere_computer.connection import WIRE_LIMIT
    from anywhere_computer.mcp_server import _reply_result
    from anywhere_computer.plugin_images import IMAGE_LIMIT

    # Envelope-size test, not a decoder test: only the PNG signature is material here.
    raw = b"\x89PNG\r\n\x1a\n" + b"x" * (IMAGE_LIMIT - 8)
    item = {"type": "image", "mimeType": "image/png",
            "data": base64.b64encode(raw).decode("ascii")}
    reply = Reply(operation_id="d" * 32, state="completed", data=_tool_result({
        "content": [item, {"type": "text", "text": "x" * 65536}],
        "structuredContent": {"text": "x" * 60000},
    }))
    wire = json.dumps(_reply_result("codex_plugin_call", reply)).encode()
    assert len(wire) < WIRE_LIMIT
    assert wire.count(item["data"].encode()) == 1
