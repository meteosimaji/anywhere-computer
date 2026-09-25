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
        if mode == 'accessibility_required_windows':
            print(json.dumps({'id': req['id'], 'error': {
                'code': 'accessibility_required'}}), flush=True)
            continue
        result = {'windows': [{'window_id': 1}]}
    elif method == 'observe':
        if mode == 'malformed_observe':
            print('{synthetic_secret_ABC123}', flush=True)
            continue
        if mode in ('window_unavailable', 'accessibility_required'):
            print(json.dumps({'id': req['id'], 'error': {
                'code': mode}}), flush=True)
            continue
        if mode == 'private_error_code':
            print(json.dumps({'id': req['id'], 'error': {
                'code': 'synthetic_secret_ABC123'}}), flush=True)
            continue
        result = {'observation_id': 'fixture-observation', 'tree': {}}
    else:
        if mode == 'element_unavailable':
            print(json.dumps({'id': req['id'], 'error': {
                'code': 'element_unavailable'}}), flush=True)
            continue
        if mode in ('changed', 'press_changed'):
            code = 'press_target_changed' if mode == 'press_changed' else 'value_changed'
            print(json.dumps({'id': req['id'], 'error': {'code': code}}), flush=True)
            continue
        counter.write_text(counter.read_text() + 'write\n' if counter.exists() else 'write\n')
        if mode == 'lost':
            sys.exit(0)
        result = ({'action_accepted': True, 'postcondition_verified': False}
                  if method == 'press' else
                  {'value_verified': True, 'persistence_verified': False})
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


@pytest.mark.parametrize("method,mode,expected", [
    ("set_value", "lost", "unknown"), ("press", "lost", "unknown"),
    ("set_value", "changed", "failed"), ("press", "changed", "failed"),
    ("press", "press_changed", "failed"),
])
async def test_native_outcome_is_durable_and_not_replayed(
        tmp_path, helper_process, mode, expected, method):
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
        request = Request(operation_id="3" * 32, tool=f"gui_native_{method}", arguments={
            **arguments, "observation_id": observed.data["observation_id"],
            "element_ref": "field", **({"value": "write once"} if method == "set_value" else {}),
        })
        first = await engine.execute(request, peer="one")
        second = await engine.execute(request, peer="one")
        assert first == second and first.state == expected
        if mode == "lost":
            assert first.data["error_code"] == "native_gui_outcome_unknown"
            assert helper_process[1].read_text() == "write\n"
            assert not engine.native_gui.entries
        else:
            assert "input was not attempted" in first.error
            assert not helper_process[1].exists()
            assert opened.data["session_id"] in engine.native_gui.entries
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


@pytest.mark.parametrize("mode", ["malformed_observe", "private_error_code"])
async def test_native_helper_errors_do_not_expose_response_content(
        tmp_path, helper_process, mode):
    helper_process[0][0] = mode
    engine = Engine(tmp_path / "state")
    try:
        opened = await engine.execute(Request(operation_id="4" * 32,
            tool="gui_native_windows", arguments={"app": "test"}), peer="one")
        assert opened.state == "completed"
        observed = await engine.execute(Request(operation_id="5" * 32,
            tool="gui_native_observe", arguments={
                "session_id": opened.data["session_id"], "app": "test", "window_id": 1,
            }), peer="one")
        assert observed.state == "failed"
        assert "synthetic_secret_ABC123" not in observed.model_dump_json()
        expected = ("Invalid native GUI response JSON" if mode == "malformed_observe"
                    else "invalid_response")
        assert expected in observed.error
        assert not engine.native_gui.entries
        assert "Open a new native GUI session" in observed.data["next_action"]
    finally:
        await engine.close()


async def test_nonfatal_helper_error_keeps_session_and_guides_reobservation(
        tmp_path, helper_process):
    helper_process[0][0] = "window_unavailable"
    engine = Engine(tmp_path / "state")
    try:
        opened = await engine.execute(Request(operation_id="6" * 32,
            tool="gui_native_windows", arguments={"app": "test"}), peer="one")
        session_id = opened.data["session_id"]
        target = {"session_id": session_id, "app": "test", "window_id": 1}
        first = await engine.execute(Request(operation_id="7" * 32,
            tool="gui_native_observe", arguments=target), peer="one")
        assert first.state == "failed"
        assert first.error == "Native GUI helper rejected request: window_unavailable"
        assert first.data["next_action"] == "Observe the target again before further input."
        assert session_id in engine.native_gui.entries
        second = await engine.execute(Request(operation_id="8" * 32,
            tool="gui_native_observe", arguments=target), peer="one")
        assert second.data["error_code"] == "window_unavailable"
        assert session_id in engine.native_gui.entries
    finally:
        await engine.close()


async def test_accessibility_error_points_to_permission_and_retains_session(
        tmp_path, helper_process):
    helper_process[0][0] = 'accessibility_required'
    engine = Engine(tmp_path / 'state')
    try:
        opened = await engine.execute(Request(operation_id='d' * 32,
            tool='gui_native_windows', arguments={'app': 'test'}), peer='one')
        session_id = opened.data['session_id']
        observed = await engine.execute(Request(operation_id='e' * 32,
            tool='gui_native_observe', arguments={
                'session_id': session_id, 'app': 'test', 'window_id': 1}), peer='one')
        assert observed.data['error_code'] == 'accessibility_required'
        assert 'Accessibility access' in observed.data['next_action']
        assert session_id in engine.native_gui.entries
    finally:
        await engine.close()


async def test_first_windows_accessibility_error_explains_permission_and_new_session(
        tmp_path, helper_process):
    helper_process[0][0] = 'accessibility_required_windows'
    engine = Engine(tmp_path / 'state')
    try:
        reply = await engine.execute(Request(operation_id='f' * 32,
            tool='gui_native_windows', arguments={'app': 'test'}), peer='one')
        assert reply.state == 'failed'
        assert reply.data['error_code'] == 'accessibility_required'
        assert 'System Settings > Privacy & Security > Accessibility' in reply.data['next_action']
        assert 'open a new native GUI session' in reply.data['next_action']
        assert not engine.native_gui.entries
    finally:
        await engine.close()


async def test_nonfatal_mutation_rejection_keeps_session_and_invalidates_observation(
        tmp_path, helper_process):
    helper_process[0][0] = "element_unavailable"
    engine = Engine(tmp_path / "state")
    try:
        opened = await engine.execute(Request(operation_id="9" * 32,
            tool="gui_native_windows", arguments={"app": "test"}), peer="one")
        session_id = opened.data["session_id"]
        target = {"session_id": session_id, "app": "test", "window_id": 1}
        observed = await engine.execute(Request(operation_id="a" * 32,
            tool="gui_native_observe", arguments=target), peer="one")
        write = {**target, "observation_id": observed.data["observation_id"],
                 "element_ref": "field", "value": "new"}
        first = await engine.execute(Request(operation_id="b" * 32,
            tool="gui_native_set_value", arguments=write), peer="one")
        assert first.state == "failed"
        assert first.data["next_action"] == "Observe the target again before further input."
        assert session_id in engine.native_gui.entries
        assert not helper_process[1].exists()
        second = await engine.execute(Request(operation_id="c" * 32,
            tool="gui_native_set_value", arguments=write), peer="one")
        assert second.data["error_code"] == "native_gui_observation_unavailable"
        assert session_id in engine.native_gui.entries
    finally:
        await engine.close()
