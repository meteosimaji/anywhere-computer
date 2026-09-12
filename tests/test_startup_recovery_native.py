import subprocess
from dataclasses import replace

import pytest

from anywhere_computer import startup_native as native
from anywhere_computer.autostart import startup_definition


def _definition(tmp_path, platform):
    return startup_definition(tmp_path, platform=platform, home=tmp_path,
                              executable=str(tmp_path / "python"), user="fixture")


@pytest.fixture(autouse=True)
def fixture_macos_user(monkeypatch):
    # These are command-construction tests; the real macOS user lookup is tested natively.
    monkeypatch.setattr(native, "_gui_domain", lambda: "gui/501")


@pytest.mark.parametrize(("state", "enabled"), [("enabled", True), ("disabled", False)])
def test_darwin_query_reads_print_disabled_state(tmp_path, monkeypatch, state, enabled):
    definition = _definition(tmp_path, "darwin")
    target = native.NativeStartup(definition)
    print_output = f'\t"{definition.name}" => {state}\n'
    service_output = f"\tpath = {definition.path}\n\tstate = running\n"
    monkeypatch.setattr(native, "_run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, service_output, "")
                        if command[1] == "print" and len(command) == 4
                        else subprocess.CompletedProcess(command, 0, print_output, ""))
    assert target.query().enabled is enabled


def test_darwin_absent_print_disabled_entry_defaults_enabled(tmp_path, monkeypatch):
    definition = _definition(tmp_path, "darwin")
    target = native.NativeStartup(definition)
    monkeypatch.setattr(native, "_checked", lambda command, **kwargs: "")
    assert target._mac_enabled() is True


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_start_rechecks_owned_identity_before_starting(tmp_path, monkeypatch, platform):
    definition = _definition(tmp_path, platform)
    target = native.NativeStartup(definition)
    snapshot = native.StartupSnapshot(True, True, False, True, "a" * 64, "xml")
    changed = replace(snapshot, fingerprint="b" * 64)
    monkeypatch.setattr(target, "query", lambda: changed)
    with pytest.raises(RuntimeError, match="changed before start"):
        target.start(snapshot)


def test_darwin_start_uses_kickstart_without_kill(tmp_path, monkeypatch):
    definition = _definition(tmp_path, "darwin")
    target = native.NativeStartup(definition)
    snapshot = native.StartupSnapshot(True, True, False, True, "a" * 64)
    monkeypatch.setattr(target, "query", lambda: snapshot)
    calls = []
    monkeypatch.setattr(native, "_checked", lambda command, **kwargs: calls.append(command) or "")
    target.start(snapshot)
    assert calls == [["/bin/launchctl", "kickstart", target._mac_target()]]


def test_linux_start_uses_systemctl_start(tmp_path, monkeypatch):
    definition = _definition(tmp_path, "linux")
    target = native.NativeStartup(definition)
    snapshot = native.StartupSnapshot(True, True, False, True, "a" * 64)
    monkeypatch.setattr(target, "query", lambda: snapshot)
    calls = []
    monkeypatch.setattr(target, "_systemd", lambda *arguments: calls.append(arguments) or "")
    target.start(snapshot)
    assert calls == [("start", definition.name + ".service")]


def test_windows_start_passes_current_xml_as_expected_value(tmp_path, monkeypatch):
    definition = _definition(tmp_path, "win32")
    target = native.NativeStartup(definition)
    snapshot = native.StartupSnapshot(True, True, False, True, "a" * 64, "owned xml")
    monkeypatch.setattr(target, "query", lambda: snapshot)
    calls = []
    monkeypatch.setattr(target, "_windows", lambda *args, **kwargs:
                        calls.append((args, kwargs)) or {})
    target.start(snapshot)
    assert calls == [(('start',), {"expected_xml": "owned xml"})]


def test_windows_script_checks_xml_before_running_task():
    assert '$request.operation -eq "start"' in native._WINDOWS_SCRIPT
    assert '$task.Xml -cne $request.expectedXml' in native._WINDOWS_SCRIPT
    assert '$null=$task.Run($null)' in native._WINDOWS_SCRIPT
