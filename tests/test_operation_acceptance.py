import asyncio
import uuid

from anywhere_computer import engine as engine_module
from anywhere_computer.engine import Engine
from anywhere_computer.mcp_server import OPERATION_META, MCPSession
from anywhere_computer.models import Empty, Request


async def ready(engine):
    async def catalog():
        return engine.catalog()

    session = MCPSession(catalog, engine.execute)
    session.initialized = session.ready = True
    return session


async def test_idle_status_does_not_block_its_own_update(tmp_path):
    engine = Engine(tmp_path)
    try:
        result = await engine.execute(Request(operation_id=uuid.uuid4().hex,
                                              tool="computer_status"))
        assert result.data["update_blocked"] is False
        assert result.data["active_resources"]["operations"] == 0
    finally:
        await engine.close()


async def test_known_id_and_early_ack_survive_lost_first_response(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "OBSERVER_WAIT_SECONDS", 0.01)
    engine = Engine(tmp_path)
    release = asyncio.Event()
    calls = []

    async def delayed(_):
        calls.append(1)
        await release.wait()
        return {"finished": True}

    engine.register("delayed", "Fixture", Empty, delayed)
    session = await ready(engine)
    known = uuid.uuid4().hex
    packet = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "delayed", "arguments": {"request_id": known},
    }}
    try:
        receipt = (await session.handle(packet))["result"]
        assert receipt["structuredContent"]["operation_id"] == known
        assert receipt["structuredContent"]["state"] == "running"
        assert receipt["isError"] is False
        # Discard the first receipt. A retry with the known ID does not repeat effects.
        assert (await session.handle(packet))["result"]["structuredContent"]["state"] == "running"
        assert len(calls) == 1
        release.set()
        await asyncio.gather(*engine.inflight.values())
        # Restore the production observer window for SQLite recovery on slower hosts.
        monkeypatch.setattr(engine_module, "OBSERVER_WAIT_SECONDS", 5.0)
        recovered = await engine.execute(Request(operation_id=uuid.uuid4().hex,
            tool="operations_get", arguments={"operation_id": known}))
        assert recovered.data["state"] == "completed"
        assert recovered.data["data"] == {"finished": True}
    finally:
        release.set()
        await engine.close()


async def test_conflicting_ids_do_not_execute_and_schema_advertises_argument(tmp_path):
    engine = Engine(tmp_path)
    session = await ready(engine)
    try:
        catalog = await session.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert all("request_id" in t["inputSchema"]["properties"]
                   for t in catalog["result"]["tools"])
        response = await session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "computer_status", "arguments": {"request_id": "a" * 32},
                       "_meta": {OPERATION_META: "b" * 32}}})
        assert response["error"]["code"] == -32602
        assert not engine.ledger.recent(10)
    finally:
        await engine.close()
