import asyncio
import json
import os

import psutil
import pytest

from anywhere_computer.connection import serve
from anywhere_computer.diagnostics import diagnose


async def test_diagnosis_does_not_initialize_missing_state(tmp_path):
    directory = tmp_path / "absent"
    assert (await diagnose(directory))["state"] == "stopped"
    assert not directory.exists()


@pytest.mark.parametrize("contents", ["[]", "null", "{broken", '{"pid":true}'])
async def test_malformed_endpoint_returns_diagnosis(tmp_path, contents):
    endpoint = tmp_path / "agent.json"
    endpoint.write_text(contents, encoding="utf-8")
    assert (await diagnose(tmp_path))["state"] == "invalid_endpoint"
    assert endpoint.read_text(encoding="utf-8") == contents


async def test_reused_pid_does_not_contact_process(tmp_path, monkeypatch):
    (tmp_path / "agent.json").write_text(
        json.dumps({"pid": os.getpid(), "process_started": 0}), encoding="utf-8"
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Stale endpoint must not access credentials")

    monkeypatch.setattr("anywhere_computer.diagnostics.local_credential", forbidden)
    assert (await diagnose(tmp_path))["state"] == "stale_endpoint"


async def test_credentials_failure_is_sanitized(tmp_path, monkeypatch):
    (tmp_path / "agent.json").write_text(
        json.dumps({"pid": os.getpid(), "process_started": psutil.Process().create_time()}),
        encoding="utf-8",
    )

    def unavailable(*args, **kwargs):
        raise RuntimeError("secret-backend-detail")

    monkeypatch.setattr("anywhere_computer.diagnostics.local_credential", unavailable)
    result = await diagnose(tmp_path)
    assert result["state"] == "credential_unavailable"
    assert "secret-backend-detail" not in json.dumps(result)


async def test_live_diagnosis_does_not_restart_or_modify_agent(tmp_path, monkeypatch):
    secret = "disposable-test-credential"
    monkeypatch.setattr(
        "anywhere_computer.diagnostics.local_credential", lambda *a, **kw: secret
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(serve(tmp_path, credential=secret, shutdown=shutdown))
    try:
        for _ in range(300):
            if (tmp_path / "agent.json").exists():
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        first = await diagnose(tmp_path)
        second = await diagnose(tmp_path)
        assert first["state"] == second["state"] == "ready"
        assert first["changed"] is False
        assert first["agent"]["instance_id"] == second["agent"]["instance_id"]
        assert secret not in json.dumps(first)
        monkeypatch.setattr(
            "anywhere_computer.diagnostics.local_credential", lambda *a, **kw: "wrong"
        )
        assert (await diagnose(tmp_path))["state"] == "unresponsive"
        monkeypatch.setattr(
            "anywhere_computer.diagnostics.local_credential", lambda *a, **kw: secret
        )
        monkeypatch.setattr("anywhere_computer.diagnostics.runtime_identity", lambda: "new")
        assert (await diagnose(tmp_path))["state"] == "different_build"
        assert not task.done()
    finally:
        shutdown.set()
        await asyncio.wait_for(task, 5)
