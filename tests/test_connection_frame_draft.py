"""The optional public UI nonce is transport scoped and never changes tool grants."""

import re

from test_workspace_ui import initialize, rpc

from anywhere_computer.mcp_server import MCPSession
from anywhere_computer.models import Reply


async def test_draft_nonce_requires_current_setup_capabilities_and_ui_negotiation():
    allowed = {"workspace_open", "connection_setup_status", "connection_setup_plan",
               "connection_setup_confirm", "files_read"}
    calls = []

    async def catalog():
        return [{"name": name} for name in sorted(allowed)]

    async def execute(request):
        calls.append(request)
        return Reply(operation_id=request.operation_id, state="completed", data={})

    first = MCPSession(catalog, execute)
    second = MCPSession(catalog, execute)
    text_only = MCPSession(catalog, execute)
    await initialize(first)
    await initialize(second)
    await initialize(text_only, ui=False)
    listed = (await rpc(first, "tools/list"))["result"]["tools"]
    assert {tool["name"] for tool in listed} == allowed
    opened = (await rpc(first, "tools/call", name="workspace_open"))["result"]
    nonce = opened["_meta"]["connectionDraftSession"]
    assert re.fullmatch("[a-f0-9]{32}", nonce)
    observed = (await rpc(first, "tools/call", name="connection_setup_status"))["result"]
    assert observed["_meta"]["connectionDraftSession"] == nonce
    other = (await rpc(second, "tools/call", name="workspace_open"))["result"]
    assert other["_meta"]["connectionDraftSession"] != nonce
    ordinary = (await rpc(first, "tools/call", name="files_read"))["result"]
    assert "_meta" not in ordinary
    text = (await rpc(text_only, "tools/call", name="workspace_open"))["result"]
    assert "_meta" not in text
    allowed.remove("connection_setup_confirm")
    revoked = (await rpc(first, "tools/call", name="workspace_open"))["result"]
    assert "connectionDraftSession" not in revoked["_meta"]
    observed = (await rpc(first, "tools/call", name="connection_setup_status"))["result"]
    assert "_meta" not in observed
    assert len(calls) == 7
