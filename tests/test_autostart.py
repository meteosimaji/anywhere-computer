import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from anywhere_computer import autostart


def definition_for(directory, platform, *, executable=None, user="S-1-5-21-123-456-789-1001"):
    return autostart.startup_definition(
        directory, platform=platform, home=directory.parent / "home",
        executable=os.path.abspath(sys.executable) if executable is None else executable, user=user,
    )


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_render_does_not_create_state_or_overwrite_existing_definition(tmp_path, platform):
    state = tmp_path / "uncreated"
    first = definition_for(state, platform)
    assert not state.exists()
    first.path.parent.mkdir(parents=True)
    first.path.write_bytes(b"foreign definition")
    again = definition_for(state, platform)
    assert again == first
    assert first.path.read_bytes() == b"foreign definition"
    assert definition_for(tmp_path / "other", platform).name != first.name


def test_launch_agent_preserves_argv_and_venv_identity(tmp_path):
    state = tmp_path / "日本語 $HOME 100%"
    executable = str(tmp_path / "venv with spaces" / "bin" / "python")
    definition = definition_for(state, "darwin", executable=executable)
    document = plistlib.loads(definition.content)
    assert document["ProgramArguments"] == [
        executable, "-B", "-I", "-X", "utf8", "-m", "anywhere_computer.cli",
        "remote-watch", "--state-dir", str(state),
    ]
    assert document["WorkingDirectory"] == str(state)
    assert document["RunAtLoad"] is True
    assert document["KeepAlive"] is False
    assert "UserName" not in document
    assert document["Umask"] == 0o077


def test_systemd_escaping_does_not_expand_environment_or_specifiers(tmp_path):
    # POSIX-only characters need not be representable as Windows filenames to be rendered.
    state = tmp_path / '日本語 $HOME 100% "quoted" \\ tail'
    definition = definition_for(state, "linux")
    text = definition.content.decode()
    command = next(line.removeprefix("ExecStart=") for line in text.splitlines()
                   if line.startswith("ExecStart="))
    # The supported quotes/backslashes are decoded by the POSIX quote parser;
    # ':' disables environment expansion; the percent specifier escape still applies.
    assert command.startswith(":")
    arguments = [part.replace("%%", "%") for part in shlex.split(command[1:])]
    assert arguments == [os.path.abspath(sys.executable), "-B", "-I", "-X", "utf8", "-m",
                         "anywhere_computer.cli",
                         "remote-watch", "--state-dir", str(state.resolve())]
    working = next(line.removeprefix("WorkingDirectory=") for line in text.splitlines()
                   if line.startswith("WorkingDirectory="))
    assert working.replace("%%", "%") == str(state.resolve()) + "/."
    assert "Restart=no\n" in text and "KillMode=control-group\n" in text
    assert "User=" not in text and "After=default.target" not in text


def test_windows_task_is_interactive_current_user_and_uses_direct_exec(tmp_path):
    state = tmp_path / "日本語 with spaces & data"
    definition = definition_for(state, "win32")
    root = ET.fromstring(definition.content)
    ns = {"t": autostart.TASK_NAMESPACE}
    principal = root.find("t:Principals/t:Principal", ns)
    assert principal is not None
    assert principal.findtext("t:UserId", namespaces=ns) == "S-1-5-21-123-456-789-1001"
    assert principal.findtext("t:LogonType", namespaces=ns) == "InteractiveToken"
    assert principal.findtext("t:RunLevel", namespaces=ns) == "LeastPrivilege"
    assert root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns) == (
        principal.findtext("t:UserId", namespaces=ns)
    )
    assert root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns) == (
        os.path.abspath(sys.executable)
    )
    assert root.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=ns) == str(state)
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=ns) == "PT0S"
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
    assert len(root.findall("t:Actions/t:Exec", ns)) == 1
    assert root.find("t:Settings/t:RestartOnFailure", ns) is None


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("invalid", ["relative/python", "", "\n", "\r", "\x00", "\x7f"])
def test_invalid_interpreter_is_rejected_before_any_write(tmp_path, platform, invalid):
    with pytest.raises(ValueError):
        definition_for(tmp_path / "state", platform, executable=invalid)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Native macOS plist validator")
def test_launch_agent_passes_native_plutil(tmp_path):
    definition = definition_for(tmp_path / "日本語 $HOME 100%", "darwin")
    exported = tmp_path / "fixture.plist"
    exported.write_bytes(definition.content)
    subprocess.run(["/usr/bin/plutil", "-lint", str(exported)], check=True, capture_output=True)


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux unit validator")
def test_systemd_unit_passes_native_validator(tmp_path):
    validator = shutil.which("systemd-analyze")
    if validator is None:
        pytest.skip("systemd-analyze is unavailable")
    interpreter = tmp_path / "python 日本語 $HOME 100%"
    interpreter.symlink_to(sys.executable)
    definition = definition_for(tmp_path / '日本語 $HOME 100% "quoted" \\ tail ', "linux",
                                executable=str(interpreter))
    exported = tmp_path / (definition.name + ".service")
    exported.write_bytes(definition.content)
    # Static parsing works without a running per-user systemd manager in CI.
    result = subprocess.run([validator, "--man=no", "verify", str(exported)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows task XML validator")
def test_windows_task_passes_native_in_memory_validation(tmp_path):
    definition = autostart.current_definition(tmp_path / "日本語 with spaces & data")
    # NewTask creates an in-memory definition. No RegisterTask call or scheduler mutation.
    script = (
        '$ErrorActionPreference="Stop"; '
        '[Console]::InputEncoding=New-Object System.Text.UTF8Encoding; '
        '[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding; '
        '$scheduler=New-Object -ComObject "Schedule.Service"; $scheduler.Connect(); '
        '$definition=$scheduler.NewTask(0); '
        '$definition.XmlText=[Console]::In.ReadToEnd(); '
        '[Console]::Out.Write($definition.XmlText)'
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        input=definition.content.decode("utf-16"), capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    parsed = ET.fromstring(result.stdout)
    ns = {"t": autostart.TASK_NAMESPACE}
    assert parsed.findtext("t:Actions/t:Exec/t:Command", namespaces=ns) == (
        os.path.abspath(sys.executable)
    )


@pytest.mark.parametrize("length", [259, 260, 261])
def test_windows_task_rejects_paths_over_schema_limit(tmp_path, length):
    prefix = str(tmp_path.anchor)
    executable = prefix + "p" * (length - len(prefix))
    if length > 260:
        with pytest.raises(ValueError, match="260 UTF-16"):
            definition_for(tmp_path / "state", "win32", executable=executable)
    else:
        assert definition_for(tmp_path / "state", "win32", executable=executable).content


@pytest.mark.parametrize("name", ["literal%name", "%TEMP%", "$literal %UNKNOWN%"])
def test_windows_scheduler_expansion_is_rejected(tmp_path, name):
    with pytest.raises(ValueError, match="scheduler expansion"):
        definition_for(tmp_path / name, "win32")
    with pytest.raises(ValueError, match="scheduler expansion"):
        definition_for(tmp_path / "state", "win32", executable=str(tmp_path / name / "python"))


def test_preview_cli_has_no_state_side_effects(tmp_path):
    state = tmp_path / "not-created"
    selected = str(tmp_path / "selected 日本語 connector")
    result = subprocess.run(
        [sys.executable, "-m", "anywhere_computer.cli", "autostart-preview",
         "--state-dir", str(state), "--connector", selected],
        check=True, capture_output=True, text=True,
    )
    preview = json.loads(result.stdout)
    assert preview["changed"] is False and preview["readiness_checked"] is False
    assert preview["registration_state"] == "unverified"
    assert "--connector" in preview["definition"]
    if sys.platform == "darwin":
        arguments = plistlib.loads(preview["definition"].encode())["ProgramArguments"]
        assert arguments[arguments.index("--connector") + 1] == selected
    assert str(state) in preview["definition"] or sys.platform == "win32"
    assert not state.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux path selection")
def test_xdg_config_home_is_respected_and_must_be_absolute(tmp_path, monkeypatch):
    configured = tmp_path / "custom config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configured))
    definition = autostart.current_definition(tmp_path / "state")
    assert definition.path.parent == configured / "systemd/user"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    with pytest.raises(ValueError, match="XDG_CONFIG_HOME must be absolute"):
        autostart.current_definition(tmp_path / "state")
    assert not configured.exists()


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_local_startup_uses_shared_watch_without_tunnel(tmp_path, platform):
    definition = autostart.startup_definition(
        tmp_path, platform=platform, home=tmp_path,
        executable=os.path.abspath(sys.executable), user="fixture-user",
        mode="local", policy_version=2,
    )
    content = definition.content.decode("utf-16" if platform == "win32" else "utf-8")
    assert "local-watch" in content
    assert "remote-watch" not in content
    assert "--connector" not in content
    assert "--persistent" not in content
    if platform == "linux":
        assert "KillMode=process\n" in content
    with pytest.raises(ValueError, match="connector"):
        autostart.startup_definition(
            tmp_path, platform=platform, home=tmp_path,
            executable=os.path.abspath(sys.executable), user="fixture-user",
            mode="local", connector=os.path.abspath(sys.executable),
        )
