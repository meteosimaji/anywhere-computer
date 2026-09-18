"""Preflight must not substitute another socket or manufacture caller metadata."""

import importlib.util
import socket
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_chat_worker.py"
SPEC = importlib.util.spec_from_file_location("chat_worker_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_requires_executor_context():
    assert probe.connection_state({}) == "endpoint_not_provided"
    assert probe.connection_state({"CODEX_APP_TOOLS_PIPE_PATH": "x"}) == "caller_not_provided"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket preflight")
def test_detects_removed_live_endpoint():
    if probe.os.name == "nt":
        pytest.skip("Windows uses named pipes")
    with tempfile.TemporaryDirectory(prefix="ac-", dir="/tmp") as raw:
        _check_endpoint(Path(raw) / "app.sock")


def _check_endpoint(endpoint):
    env = {"CODEX_APP_TOOLS_PIPE_PATH": str(endpoint), "CODEX_THREAD_ID": "test-caller"}
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint))
        listener.listen()
        assert probe.connection_state(env) == "ready_to_probe"
        endpoint.unlink()
        assert probe.connection_state(env) == "endpoint_missing"
    endpoint.write_text("not a socket")
    assert probe.connection_state(env) == "endpoint_not_socket"


def snapshot(prompt="new request", status="idle", old_only=False):
    def turn(key, text):
        return {"id": key, "status": "completed", "error": None, "items": [
            {"id": key, "type": "userMessage",
             "content": [{"type": "text", "text": text}]},
            {"id": key + "-answer", "type": "agentMessage", "text": "42"},
        ]}
    old = turn("old", "old request")
    return {"thread": {"id": "chat", "kind": "chatgpt", "status": {"type": status}},
            "turns": [old] if old_only else [turn("new", prompt), old]}


def test_reply_requires_new_matching_turn():
    assert probe.matching_reply(snapshot(old_only=True), "chat", "old", "new request") is None
    assert probe.matching_reply(snapshot(), "chat", "old", "different") is None
    assert probe.matching_reply(snapshot(), "other-chat", "old", "new request") is None
    assert probe.matching_reply(snapshot(), "chat", "absent", "new request") is None
    assert probe.matching_reply(snapshot(status="active"), "chat", "old", "new request") is None
    result = probe.matching_reply(snapshot(), "chat", "old", "new request")
    assert result == {"user_message_id": "new", "answer_message_id": "new-answer", "text": "42"}


@pytest.mark.parametrize("change", ["truncated", "missing_answer", "duplicate", "work"])
def test_ambiguous_or_incomplete_reply_is_unconfirmed(change):
    data = snapshot()
    if change == "truncated":
        data["turns"][0]["items"][1]["truncated"] = True
    elif change == "missing_answer":
        data["turns"][0]["items"].pop()
    elif change == "duplicate":
        import copy
        duplicate = copy.deepcopy(data["turns"][0])
        duplicate["id"] = duplicate["items"][0]["id"] = "another"
        data["turns"].insert(0, duplicate)
    else:
        data["thread"]["kind"] = "codex"
    assert probe.matching_reply(data, "chat", "old", "new request") is None


async def test_waits_through_stale_snapshot_without_resending():
    values = iter([snapshot(old_only=True), snapshot(status="active"), snapshot()])

    async def read():
        return next(values)

    result = await probe.wait_for_reply(read, "chat", "old", "new request", 1, .001)
    assert result["state"] == "reply_observed"
    assert result["read_attempts"] == 3


async def test_read_timeout_is_unknown_not_failed_send():
    import asyncio

    async def read():
        await asyncio.sleep(10)

    result = await probe.wait_for_reply(read, "chat", "old", "new request", .01)
    assert result == {"state": "reply_unconfirmed", "read_attempts": 1, "resend": False}
