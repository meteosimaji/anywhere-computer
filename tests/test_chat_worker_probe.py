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
