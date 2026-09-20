import asyncio
import hashlib
import json
import sys

import pytest

from anywhere_computer import audio_status
from anywhere_computer.engine import Engine
from anywhere_computer.models import Request


@pytest.mark.parametrize("platform,expected", [("win32", "unsupported"), ("darwin", "unavailable")])
async def test_audio_status_without_helper_does_not_launch(
    tmp_path, monkeypatch, platform, expected,
):
    monkeypatch.setattr(audio_status.sys, "platform", platform)
    monkeypatch.setattr(audio_status.sys, "prefix", str(tmp_path / "runtime"))

    async def forbidden(*args):
        pytest.fail("Inspection without a helper must not spawn a process")

    monkeypatch.setattr(audio_status, "_query", forbidden)
    engine = Engine(tmp_path / "state")
    try:
        result = await engine.execute(Request(operation_id="a" * 32, tool="audio_status"))
        assert result.state == "completed"
        assert result.data["state"] == expected
        assert result.data["capture_started"] is False
        assert result.data["capture_tool_available"] is False
        assert engine.tools["audio_status"].read_only
    finally:
        await engine.close()


async def test_audio_manifest_and_read_only_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_status.sys, "platform", "darwin")
    monkeypatch.setattr(audio_status.sys, "prefix", str(tmp_path / "runtime"))
    helper = tmp_path / "native/anywhere-audio"
    helper.parent.mkdir()
    helper.write_bytes(b"fixture native binary")
    helper.chmod(0o755)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": {
        "native/anywhere-audio": hashlib.sha256(helper.read_bytes()).hexdigest(),
    }}))
    calls = []

    async def query(command):
        calls.append(command)
        if command[-1] == "--check":
            return {"state": "permission_check", "capture_started": False,
                    "screen_capture_allowed": True, "microphone_allowed": False}
        return {"state": "device_list", "capture_started": False,
                "devices": [{"id": "selected-id", "name": "Input"}]}

    monkeypatch.setattr(audio_status, "_query", query)
    result = await audio_status.inspect_audio()
    assert result["devices"] == [{"id": "selected-id", "name": "Input"}]
    assert result["microphone_allowed"] is False
    assert calls == [[str(helper), "--check"], [str(helper), "--list-devices"]]
    async def malformed(command):
        return {"state": "permission_check", "capture_started": True}

    monkeypatch.setattr(audio_status, "_query", malformed)
    with pytest.raises(ValueError, match="inspection response"):
        await audio_status.inspect_audio()
    monkeypatch.setattr(audio_status, "_query", query)
    helper.write_bytes(b"changed binary")
    with pytest.raises(ValueError, match="manifest"):
        await audio_status.inspect_audio()
    assert len(calls) == 2


@pytest.mark.parametrize("source,error", [
    ("import time;time.sleep(10)", TimeoutError),
    ("print('x'*70000)", ValueError),
])
async def test_query_bounds_and_reaps_child(monkeypatch, source, error):
    children = []
    spawn = asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    with pytest.raises(error):
        await audio_status._query([sys.executable, "-c", source], timeout=2)
    assert len(children) == 1 and children[0].returncode is not None
