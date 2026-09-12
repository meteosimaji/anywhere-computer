"""子プラグインのエラーと、操作台帳上の完了状態を区別する回帰テスト。"""

import json

import pytest

from anywhere_computer.mcp_server import OPERATION_META, MCPSession
from anywhere_computer.models import Reply


@pytest.mark.asyncio
@pytest.mark.parametrize("name,state,data,expected", [
    ("codex_plugin_call", "completed", {"is_error": True}, True),
    ("codex_plugin_call", "completed", {"is_error": False}, False),
    ("codex_plugin_call", "completed", {}, False),
    ("codex_plugin_call", "completed", {"is_error": None}, False),
    ("codex_plugin_call", "completed", {"is_error": "false"}, False),
    ("codex_plugin_call", "completed", {"is_error": 1}, False),
    ("files_read", "completed", {"is_error": True}, False),
    ("codex_plugin_tools", "completed", {"is_error": True}, False),
    ("codex_plugin_call", "failed", {"is_error": False}, True),
    ("codex_plugin_call", "unknown", {"is_error": False}, True),
    ("codex_plugin_call", "running", {"is_error": False}, False),
    ("files_read", "failed", {}, True),
])
async def test_plugin_error_flag_does_not_replace_operation_state(name, state, data, expected):
    async def catalog():
        return [{"name": name}]

    calls = []

    async def execute(request):
        calls.append(request)
        return Reply(operation_id=request.operation_id, state=state, data=data)

    session = MCPSession(catalog, execute)
    # 初期化・UI・認証経路ではなく、tools/call 応答の組立てを単体検証する。
    session.initialized = session.ready = True
    response = await session.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    })
    result = response["result"]
    assert result["isError"] is expected
    assert result["structuredContent"]["state"] == state
    assert result["structuredContent"]["data"] == data
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_plugin_failure_preserves_operation_id_and_diagnostics():
    async def catalog():
        return [{"name": "codex_plugin_call"}]

    operation_id = "a" * 32
    data = {
        "is_error": True,
        "content": [{"type": "text", "text": "プラグイン側の入力検証エラー"}],
        "truncated": False,
    }
    calls = []

    async def execute(request):
        calls.append(request)
        return Reply(operation_id=request.operation_id, state="completed", data=data)

    session = MCPSession(catalog, execute)
    session.initialized = session.ready = True
    response = await session.handle({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {
            "name": "codex_plugin_call", "arguments": {},
            "_meta": {OPERATION_META: operation_id},
        },
    })
    result = response["result"]
    assert response["id"] == 7
    assert result["isError"] is True
    assert result["structuredContent"]["operation_id"] == operation_id
    assert result["structuredContent"]["state"] == "completed"
    assert result["structuredContent"]["data"] == data
    assert calls[0].operation_id == operation_id
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_transport_failure_is_unknown_without_retry():
    async def catalog():
        return [{"name": "codex_plugin_call"}]

    calls = []

    async def execute(request):
        calls.append(request)
        raise ConnectionError("fixture disconnect")

    session = MCPSession(catalog, execute)
    session.initialized = session.ready = True
    response = await session.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "codex_plugin_call"},
    })
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["state"] == "unknown"
    assert "operations_get" in result["structuredContent"]["error"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_catalog_permission_still_prevents_dispatch():
    async def catalog():
        return [{"name": "files_read"}]

    async def execute(request):
        pytest.fail("非公開ツールを実行してはならない")

    session = MCPSession(catalog, execute)
    session.initialized = session.ready = True
    response = await session.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "codex_plugin_call"},
    })
    assert response["error"]["code"] == -32602
