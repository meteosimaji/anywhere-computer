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
