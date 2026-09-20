import asyncio
import json
import sys

import pytest

from anywhere_computer import native_gui
from anywhere_computer.engine import Engine
from anywhere_computer.models import Request

PROGRAM = r'''
import json, sys
from pathlib import Path
counter = Path(sys.argv[1])
mode = sys.argv[2]
for line in sys.stdin:
    req = json.loads(line)
    method = req['method']
    if method == 'windows':
        result = {'windows': [{'window_id': 1}]}
    elif method == 'observe':
        result = {'observation_id': 'fixture-observation', 'tree': {}}
    else:
        if mode == 'changed':
            print(json.dumps({'id': req['id'], 'error': {'code': 'value_changed'}}), flush=True)
            continue
        counter.write_text(counter.read_text() + 'write\n' if counter.exists() else 'write\n')
        if mode == 'lost':
            sys.exit(0)
        result = {'value_verified': True, 'persistence_verified': False}
    print(json.dumps({'id': req['id'], 'result': result}), flush=True)
'''


@pytest.fixture
def helper_process(monkeypatch, tmp_path):
    children = []
    spawn = asyncio.create_subprocess_exec
    mode = ["normal"]
    counter = tmp_path / "effects"
    marker = tmp_path / "fixture-native-helper"

    async def start(*args, **kwargs):
        if args != (str(marker),):
            return await spawn(*args, **kwargs)
        process = await spawn(sys.executable, "-u", "-c", PROGRAM,
                              str(counter), mode[0], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(native_gui, "installed_helper", lambda: marker)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    return mode, counter, children


async def test_owner_binding_and_cross_session_snapshot_invalidation(helper_process):
    gui = native_gui.NativeGUI()
    try:
        first = await gui.windows(native_gui.NativeApp(app="test"), owner="one")
        second = await gui.windows(native_gui.NativeApp(app="test"), owner="two")
        a = native_gui.NativeObserve(session_id=first["session_id"], app="test", window_id=1)
        b = native_gui.NativeObserve(session_id=second["session_id"], app="test", window_id=1)
        with pytest.raises(ValueError, match="session unavailable"):
            await gui.observe(a, owner="two")
        await gui.observe(a, owner="one")
        await gui.observe(b, owner="two")
        write_a = native_gui.NativeSetValue(**a.model_dump(),
            observation_id="fixture-observation", element_ref="field", value="日本語")
        assert (await gui.set_value(write_a, owner="one"))["value_verified"]
        write_b = native_gui.NativeSetValue(**b.model_dump(),
            observation_id="fixture-observation", element_ref="field", value="other")
        with pytest.raises(ValueError, match="observation unavailable"):
            await gui.set_value(write_b, owner="two")
        assert helper_process[1].read_text() == "write\n"
    finally:
        await gui.close()
    assert all(p.returncode is not None for p in helper_process[2])


@pytest.mark.parametrize("mode,expected", [("lost", "unknown"), ("changed", "failed")])
async def test_native_outcome_is_durable_and_not_replayed(tmp_path, helper_process, mode, expected):
    helper_process[0][0] = mode
    engine = Engine(tmp_path / "state")
    try:
        opened = await engine.execute(Request(operation_id="1" * 32, tool="gui_native_windows",
                                             arguments={"app": "test"}), peer="one")
        assert opened.state == "completed"
        arguments = {"session_id": opened.data["session_id"], "app": "test", "window_id": 1}
        observed = await engine.execute(Request(operation_id="2" * 32,
            tool="gui_native_observe", arguments=arguments), peer="one")
        assert observed.state == "completed"
        request = Request(operation_id="3" * 32, tool="gui_native_set_value", arguments={
            **arguments, "observation_id": observed.data["observation_id"],
            "element_ref": "field", "value": "write once",
        })
        first = await engine.execute(request, peer="one")
        second = await engine.execute(request, peer="one")
        assert first == second and first.state == expected
        if mode == "lost":
            assert first.data["error_code"] == "native_gui_outcome_unknown"
            assert helper_process[1].read_text() == "write\n"
        else:
            assert "input was not attempted" in first.error
            assert not helper_process[1].exists()
        assert not engine.native_gui.entries
    finally:
        await engine.close()


def test_manifest_mismatch_rejected_before_helper_launch(tmp_path, monkeypatch):
    from types import SimpleNamespace

    helper = tmp_path / "native/anywhere-gui"
    helper.parent.mkdir()
    helper.write_bytes(b"changed")
    helper.chmod(0o755)
    (tmp_path / "manifest.json").write_text(json.dumps({"files": {
        "native/anywhere-gui": "0" * 64,
    }}))
    monkeypatch.setattr(native_gui, "sys", SimpleNamespace(
        platform="darwin", prefix=str(tmp_path / "runtime")))
    with pytest.raises(ValueError, match="manifest"):
        native_gui.installed_helper()
