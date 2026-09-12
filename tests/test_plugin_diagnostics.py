import json

import pytest

from anywhere_computer.plugin_diagnostics import PluginDiagnostics, rpc_diagnostic


def test_sender_authentication_failure_preserves_classification_not_message():
    result = rpc_diagnostic({
        "code": -10000,
        "message": "Sender process is not authenticated: private-context",
        "data": {"context": "private-context"},
    })
    assert result["rpc_code"] == -10000
    assert result["message_kind"] == "sender_process_not_authenticated"
    assert "private-context" not in json.dumps(result)


@pytest.mark.parametrize("code", [True, "-32602", 2**80, None])
def test_invalid_rpc_code_and_freeform_data_are_not_retained(code):
    result = rpc_diagnostic({"code": code, "message": "private-value",
                             "data": {"password": "private-value"}})
    assert result["rpc_code"] is None
    assert result["data_present"] is True
    assert "private-value" not in json.dumps(result)


def test_diagnostic_storage_is_bounded_and_does_not_guess_secret_patterns():
    diagnostic = PluginDiagnostics()
    for _ in range(1000):
        diagnostic.feed(b"arbitrary unlabelled secret\nPermission denied\xff")
    result = diagnostic.snapshot()
    assert len(result["stderr_events"]) == 32
    assert result["stderr_chunks_seen"] == 1000
    assert result["events_dropped"] is True
    assert result["stderr_events"][0]["message_kind"] == "permission_denied"
    assert "arbitrary" not in json.dumps(result)
    assert len(json.dumps(result)) < 4096
