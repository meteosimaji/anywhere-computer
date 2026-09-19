"""Preflight must not substitute another socket or manufacture caller metadata."""

import importlib.util
import socket
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_subchat.py"
SPEC = importlib.util.spec_from_file_location("subchat_probe", SCRIPT)
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


def test_first_reply_uses_observed_submission_identity_without_baseline():
    data = snapshot()
    data["turns"] = data["turns"][:1]
    # The baseline-only mode cannot collect the first reply in a new Chat.
    assert probe.matching_reply(data, "chat", "old", "new request") is None
    result = probe.matching_reply(data, "chat", None, "new request", submitted_user_id="new")
    assert result == {"user_message_id": "new", "answer_message_id": "new-answer", "text": "42"}
    assert probe.matching_reply(data, "chat", None, "new request",
                                submitted_user_id="absent") is None
    assert probe.matching_reply(data, "other", None, "new request",
                                submitted_user_id="new") is None
    assert probe.matching_reply(data, "chat", None, "different",
                                submitted_user_id="new") is None


@pytest.mark.parametrize("change", ["active", "truncated", "missing", "duplicate", "wrong_id"])
def test_first_reply_rejects_uncertain_receipts(change):
    import copy
    data = snapshot()
    if change == "active":
        data["thread"]["status"] = {"type": "active"}
    elif change == "truncated":
        data["turns"][0]["items"][1]["truncated"] = True
    elif change == "missing":
        data["turns"][0]["items"].pop()
    elif change == "duplicate":
        data["turns"].append(copy.deepcopy(data["turns"][0]))
    else:
        data["turns"][0]["items"][0]["id"] = "different"
    assert probe.matching_reply(data, "chat", None, "new request",
                                submitted_user_id="new") is None


async def test_first_reply_waits_for_acknowledged_message():
    values = iter([snapshot(old_only=True), snapshot()])

    async def read():
        return next(values)

    result = await probe.wait_for_reply(read, "chat", None, "new request", 1, .001,
                                        submitted_user_id="new")
    assert result["state"] == "reply_observed"
    assert result["user_message_id"] == "new"
    assert result["read_attempts"] == 2


def test_identity_selectors_are_exclusive():
    with pytest.raises(ValueError, match="exactly one"):
        probe.matching_reply(snapshot(), "chat", None, "new request")
    with pytest.raises(ValueError, match="exactly one"):
        probe.matching_reply(snapshot(), "chat", "old", "new request", submitted_user_id="new")


@pytest.mark.parametrize("state", ["missing_answer", "failed", "truncated_answer"])
def test_duplicate_prompt_is_ambiguous_even_if_only_one_answer_is_usable(state):
    import copy
    data = snapshot()
    duplicate = copy.deepcopy(data["turns"][0])
    duplicate["id"] = duplicate["items"][0]["id"] = "second-send"
    duplicate["items"][1]["id"] = "second-answer"
    if state == "missing_answer":
        duplicate["items"].pop()
    elif state == "failed":
        duplicate["status"] = "failed"
        duplicate["error"] = {"message": "interrupted"}
    else:
        duplicate["items"][1]["truncated"] = True
    data["turns"].insert(0, duplicate)
    assert probe.matching_reply(data, "chat", "old", "new request") is None
    # An actual submission ID resolves the ambiguity without replaying either send.
    result = probe.matching_reply(data, "chat", None, "new request", submitted_user_id="new")
    assert result["user_message_id"] == "new"


def test_reused_answer_identity_is_not_a_new_receipt():
    data = snapshot()
    data['turns'][0]['items'][1]['id'] = data['turns'][1]['items'][1]['id']
    assert probe.matching_reply(data, 'chat', 'old', 'new request') is None
    assert probe.matching_reply(data, 'chat', None, 'new request',
                                submitted_user_id='new') is None


async def test_thinking_timeout_can_resume_reading_same_submission():
    thinking = snapshot(status="active")
    thinking["turns"][0]["status"] = "inProgress"
    thinking["turns"][0]["items"][1]["text"] = "Unfinished draft"
    reads = 0

    async def read_thinking():
        nonlocal reads
        reads += 1
        return thinking

    result = await probe.wait_for_reply(
        read_thinking, "chat", None, "new request", .02, .001,
        submitted_user_id="new",
    )
    assert reads >= 1
    assert result["state"] == "reply_unconfirmed"
    assert result["resend"] is False
    assert "text" not in result
    # Resume observation only. No new prompt, conversation, or stop callback exists.
    values = iter([thinking, thinking, snapshot()])

    async def read_later():
        return next(values)

    recovered = await probe.wait_for_reply(
        read_later, "chat", None, "new request", 1, .001,
        submitted_user_id="new",
    )
    assert recovered["read_attempts"] == 3
    assert recovered["user_message_id"] == "new"
    assert recovered["text"] == "42"


def test_transport_failure_redacts_rpc_payload_and_preserves_stage():
    import json

    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    error = ExceptionGroup("private group text", [
        McpError(ErrorData(code=-32000, message="private credential text",
                           data={"token": "secret-token"})),
    ])
    result = probe.transport_failure(error, "catalog")
    assert result["failure_stage"] == "catalog"
    detail = result["failures"][0]
    assert detail["rpc_code"] == -32000 and detail["data_present"] is True
    assert detail["content_redacted"] is True
    assert "private" not in json.dumps(result) and "secret-token" not in json.dumps(result)


def test_transport_failure_does_not_hide_programming_errors():
    with pytest.raises(TypeError, match="contract defect"):
        probe.transport_failure(ExceptionGroup("group", [
            ConnectionError("closed"), TypeError("contract defect"),
        ]), "read")
    assert probe.transport_failure(TimeoutError(), "initialize") == {
        "failure_stage": "initialize", "failure_kind": "timeout",
    }


async def test_successful_catalog_with_close_failure_reports_cleanup(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import mcp
    import mcp.client.stdio

    @asynccontextmanager
    async def transport(_):
        yield None, None

    class Session:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            raise ConnectionError("close failed after successful catalog")

        async def initialize(self):
            pass

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    monkeypatch.setattr(probe, "connection_state", lambda _: "ready_to_probe")
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", transport)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    result = await probe.probe(tmp_path / "server.mjs", None)
    assert result["state"] == "transport_failed"
    assert result["diagnostic"]["failure_stage"] == "cleanup"


async def test_whole_probe_deadline_cancels_only_local_observation():
    import asyncio

    entered = False
    cleaned = False

    async def hanging_catalog():
        nonlocal entered, cleaned
        entered = True
        try:
            await asyncio.Event().wait()
        finally:
            cleaned = True

    result = await probe.bounded_probe(hanging_catalog, .01)
    assert entered and cleaned
    assert result == {"state": "probe_timeout", "inference_requested": False,
                      "send_tested": False, "ordinary_chat_creation_tested": False,
                      "resend": False}


async def test_whole_probe_preserves_result_and_programming_errors():
    async def ready():
        return {"state": "catalog_received"}

    assert await probe.bounded_probe(ready, 1) == {"state": "catalog_received"}

    async def broken():
        raise TypeError("programming defect")

    with pytest.raises(TypeError, match="programming defect"):
        await probe.bounded_probe(broken, 1)
