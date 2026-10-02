"""Linux host configuration must not silently select another binary or user profile."""

import asyncio
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from anywhere_computer import browser_configuration as configuration
from anywhere_computer.browser_control import BrowserControl, BrowserStartupUnavailable


@pytest.fixture
def linux(monkeypatch):
    if os.name == "nt":
        pytest.skip("Linux file ownership/mode contract requires POSIX")
    monkeypatch.setattr(configuration.sys, "platform", "linux")


@pytest.fixture
def chrome(tmp_path, linux):
    path = tmp_path / "Chrome with spaces 日本語"
    path.write_bytes(b"\x7fELFtest-only-placeholder")
    path.chmod(0o755)
    return path


@pytest.fixture
def version_check(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"Google Chrome 154.0.8053.9\n")

    monkeypatch.setattr(configuration.subprocess, "run", run)
    return calls


def test_default_diagnosis_does_not_create_state_or_execute(tmp_path, linux, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Metadata diagnosis must not execute any browser")

    monkeypatch.setattr(configuration.subprocess, "run", forbidden)
    directory = tmp_path / "missing"
    assert configuration.configure_browser(directory)["selection"] == "platform_default"
    assert not directory.exists()


def test_configuration_validates_version_and_pins_binary(chrome, version_check, tmp_path):
    directory = tmp_path / "state"
    report = configuration.configure_browser(directory, executable=str(chrome))
    assert version_check == [([str(chrome), "--version"], {
        "capture_output": True, "timeout": 5, "check": False,
    })]
    assert report["selection"] == "configured"
    assert report["launch_verified"] is False
    assert report["publisher_verified"] is False
    assert configuration.selected_chrome(directory) == chrome
    saved = directory / "browser.json"
    assert saved.stat().st_mode & 0o777 == 0o600
    assert json.loads(saved.read_text())["version"] == "Google Chrome 154.0.8053.9"
    chrome.write_bytes(b"\x7fELFreplaced-browser")
    with pytest.raises(ValueError, match="changed"):
        configuration.selected_chrome(directory)
    assert saved.exists()
    assert configuration.configure_browser(directory, clear=True)["selection"] == "platform_default"


@pytest.mark.parametrize("kind", [
    "relative", "absent", "directory", "script", "mode", "owner", "setuid", "not_executable",
])
def test_unsafe_paths_refused_before_execution(chrome, version_check, tmp_path, monkeypatch, kind):
    value = str(chrome)
    if kind == "relative":
        value = chrome.name
    elif kind == "absent":
        value = str(tmp_path / "absent")
    elif kind == "directory":
        value = str(tmp_path)
    elif kind == "script":
        chrome.write_bytes(b"#!/bin/sh\necho Google Chrome 154.0.8053.9\n")
    elif kind == "mode":
        chrome.chmod(0o777)
    elif kind == "setuid":
        chrome.chmod(0o4755)
    elif kind == "not_executable":
        chrome.chmod(0o644)
    else:
        monkeypatch.setattr(configuration.os, "getuid", lambda: 999999)
    with pytest.raises((OSError, ValueError)):
        configuration.configure_browser(tmp_path / "state", executable=value)
    assert not version_check
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("result", ["timeout", "error", "wrong_brand", "nonzero", "changed"])
def test_failed_version_probe_preserves_previous_selection(
    chrome, version_check, tmp_path, monkeypatch, result,
):
    directory = tmp_path / "state"
    configuration.configure_browser(directory, executable=str(chrome))
    original = (directory / "browser.json").read_bytes()

    def fail(*args, **kwargs):
        if result == "timeout":
            raise subprocess.TimeoutExpired(args[0], 5, output=b"private-output")
        if result == "error":
            raise OSError("private-path")
        if result == "changed":
            chrome.write_bytes(b"\x7fELFmodified")
        return SimpleNamespace(returncode=1 if result == "nonzero" else 0,
                               stdout=b"Chromium 154.0.8053.9" if result == "wrong_brand" else
                               b"Google Chrome 154.0.8053.9")

    monkeypatch.setattr(configuration.subprocess, "run", fail)
    with pytest.raises(ValueError) as error:
        configuration.configure_browser(directory, executable=str(chrome))
    assert "private" not in str(error.value)
    assert (directory / "browser.json").read_bytes() == original


@pytest.mark.parametrize("kind", ["malformed", "extra", "symlink", "public"])
def test_invalid_selection_cannot_fall_back(chrome, version_check, tmp_path, kind):
    directory = tmp_path / "state"
    configuration.configure_browser(directory, executable=str(chrome))
    saved = directory / "browser.json"
    if kind == "malformed":
        saved.write_text("private-invalid-json")
    elif kind == "extra":
        value = json.loads(saved.read_text())
        value["args"] = ["--no-sandbox"]
        saved.write_text(json.dumps(value))
    elif kind == "symlink":
        saved.rename(directory / "other.json")
        saved.symlink_to(directory / "other.json")
    else:
        saved.chmod(0o644)
    assert configuration.browser_configuration(directory)["selection"] == "invalid"
    control = BrowserControl(directory=directory)
    with pytest.raises(BrowserStartupUnavailable, match="browser-configure"):
        asyncio.run(control.open(owner="fixture"))
    assert control.entries == {}


async def test_selected_chrome_passed_to_launch_with_sandbox_and_no_channel(
    chrome, version_check, tmp_path, monkeypatch,
):
    directory = tmp_path / "state"
    configuration.configure_browser(directory, executable=str(chrome))
    launches = []
    stopped = []

    class Driver:
        chromium = None

        async def start(self):
            self.chromium = self
            return self

        async def launch(self, **kwargs):
            launches.append(kwargs)
            raise OSError("fixture stops before creating a tab")

        async def stop(self):
            stopped.append(True)

    monkeypatch.setattr("playwright.async_api.async_playwright", Driver)
    control = BrowserControl(directory=directory)
    with pytest.raises(BrowserStartupUnavailable):
        await control.open(owner="fixture")
    assert launches == [{"headless": True, "executable_path": str(chrome),
                         "chromium_sandbox": True}]
    assert stopped == [True]
    assert not control.entries


@pytest.mark.parametrize("system", ["darwin", "win32"])
def test_other_platforms_keep_the_existing_browser_default(monkeypatch, tmp_path, system):
    monkeypatch.setattr(configuration.sys, "platform", system)
    (tmp_path / "browser.json").write_text("ignored-linux-only-selection")
    assert configuration.selected_chrome(tmp_path) is None
    assert configuration.browser_configuration(tmp_path)["channel"] == (
        "msedge" if system == "win32" else "chrome"
    )
    with pytest.raises(ValueError, match="requires Linux"):
        configuration.configure_browser(tmp_path, executable="/selected/chrome")


def test_cli_configuration_does_not_start_engine_or_create_credentials(
    chrome, version_check, tmp_path, monkeypatch, capsys,
):
    from anywhere_computer import cli

    def forbidden(*args, **kwargs):
        pytest.fail("Local browser configuration must not initialize the engine")

    directory = tmp_path / "state"
    monkeypatch.setattr(cli, "ensure_agent", forbidden)
    monkeypatch.setattr(cli, "exchange", forbidden)
    monkeypatch.setattr(configuration.sys, "argv", [
        "anywhere", "browser-configure", "--state-dir", str(directory),
        "--chrome-executable", str(chrome),
    ])
    cli.main()
    assert json.loads(capsys.readouterr().out)["selection"] == "configured"
    assert not (directory / "agent.json").exists()
    assert not (directory / "operations.sqlite3").exists()


def test_cli_cannot_apply_chrome_option_to_other_commands(monkeypatch, tmp_path):
    from anywhere_computer import cli

    monkeypatch.setattr(configuration.sys, "argv", [
        "anywhere", "start", "--state-dir", str(tmp_path / "state"),
        "--chrome-executable", "/private/value",
    ])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert not (tmp_path / "state").exists()


def test_chromium_requires_explicit_product_selection(chrome, tmp_path, monkeypatch):
    monkeypatch.setattr(configuration.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=b"Chromium 145.0.7632.6\n",
    ))
    directory = tmp_path / "state"
    with pytest.raises(ValueError, match="Google Chrome"):
        configuration.configure_browser(directory, executable=str(chrome))
    assert not directory.exists()
    report = configuration.configure_browser(directory, chromium_executable=str(chrome))
    assert report["selection"] == "configured"
    assert configuration.selected_chrome(directory) == chrome
    chrome.write_bytes(b"\x7fELFreplaced")
    with pytest.raises(ValueError, match="changed"):
        configuration.selected_chrome(directory)


def test_chromium_selection_rejects_chrome_without_overwriting(chrome, tmp_path, version_check):
    directory = tmp_path / "state"
    configuration.configure_browser(directory, executable=str(chrome))
    original = (directory / "browser.json").read_bytes()
    with pytest.raises(ValueError, match="Chromium"):
        configuration.configure_browser(directory, chromium_executable=str(chrome))
    assert (directory / "browser.json").read_bytes() == original


@pytest.mark.parametrize("command", ["browser-configure", "start"])
def test_chromium_cli_has_local_only_scope(chrome, tmp_path, monkeypatch, capsys, command):
    from anywhere_computer import cli

    monkeypatch.setattr(configuration.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=b"Chromium 145.0.7632.6\n",
    ))
    directory = tmp_path / "state"
    monkeypatch.setattr(configuration.sys, "argv", [
        "anywhere", command, "--state-dir", str(directory), "--chromium-executable", str(chrome),
    ])
    if command == "start":
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
        assert not directory.exists()
    else:
        cli.main()
        assert json.loads(capsys.readouterr().out)["selection"] == "configured"
        assert not (directory / "agent.json").exists()


@pytest.mark.parametrize("tool,arguments", [
    ("browser_open", {"executable_path": "/untrusted/program"}),
    ("browser_open", {"chrome_executable": "/untrusted/program"}),
    ("settings_update", {"key": "chrome_executable", "value": "/untrusted/program"}),
    ("browser-configure", {"executable": "/untrusted/program"}),
])
async def test_tool_calls_cannot_select_or_reconfigure_a_browser(
    tmp_path, monkeypatch, tool, arguments,
):
    import uuid

    from anywhere_computer.engine import Engine
    from anywhere_computer.models import Request

    async def forbidden(*args, **kwargs):
        pytest.fail("Rejected executable injection must not start a browser")

    engine = Engine(tmp_path / "state")
    monkeypatch.setattr(engine.browser, "open", forbidden)
    try:
        reply = await engine.execute(Request(operation_id=uuid.uuid4().hex,
                                             tool=tool, arguments=arguments), peer="remote-owner")
        assert reply.state == "failed"
        assert "untrusted" not in reply.model_dump_json()
        if tool != "browser-configure":
            assert reply.data["error_code"] == "invalid_parameter"
            assert reply.data["dispatched"] is False
        else:
            assert reply.error == "Unknown tool"
        assert not (tmp_path / "state/browser.json").exists()
        assert "browser-configure" not in {row["name"] for row in engine.catalog()}
    finally:
        await engine.close()


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("existing", [False, True])
async def test_browser_download_cannot_create_host_selection_before_page_input(
    linux, tmp_path, monkeypatch, alias, existing,
):
    import uuid

    from anywhere_computer.models import BrowserDownload, BrowserTarget

    directory = tmp_path / "state"
    directory.mkdir()
    path = directory / "browser.json"
    if existing:
        path.write_bytes(b"existing owner selection")
    if alias:
        redirect = tmp_path / "redirect"
        redirect.symlink_to(directory, target_is_directory=True)
        path = redirect / "browser.json"
    control = BrowserControl(directory=directory)
    entry = SimpleNamespace(lock=asyncio.Lock())
    monkeypatch.setattr(control, "_entry", lambda *_: entry)
    monkeypatch.setattr(control, "_check_snapshot", lambda *_: None)

    def forbidden(*args, **kwargs):
        pytest.fail("A reserved download path must be refused before locating or clicking the page")

    monkeypatch.setattr(control, "_target_locator", forbidden)
    args = BrowserDownload(session_id=uuid.uuid4().hex, tab_id=uuid.uuid4().hex,
                           snapshot_id=uuid.uuid4().hex, path=str(path),
                           target=BrowserTarget(selector="#download"))
    with pytest.raises(ValueError, match="local Chrome selection"):
        await control.download(args, owner="browser-only")
    if existing:
        assert (directory / "browser.json").read_bytes() == b"existing owner selection"
    else:
        assert not (directory / "browser.json").exists()


def test_unrelated_download_destinations_remain_available(linux, tmp_path):
    directory = tmp_path / "state"
    configuration.reject_configuration_download(directory, tmp_path / "browser.json")
    configuration.reject_configuration_download(directory, directory / "ordinary-download.txt")


@pytest.mark.parametrize("existing", [False, True])
async def test_browser_download_rechecks_a_parent_alias_after_page_input(
    linux, tmp_path, monkeypatch, existing,
):
    import uuid
    from contextlib import asynccontextmanager

    from anywhere_computer.browser_control import BrowserActionUnknown
    from anywhere_computer.models import BrowserDownload, BrowserTarget

    directory = tmp_path / "state"
    directory.mkdir()
    saved = directory / "browser.json"
    if existing:
        saved.write_bytes(b"existing owner selection")
    safe = tmp_path / "downloads"
    safe.mkdir()
    redirect = tmp_path / "redirect"
    redirect.symlink_to(safe, target_is_directory=True)
    source = tmp_path / "received"
    source.write_bytes(b"download cannot select a browser")
    clicks = []

    async def received_path():
        return source

    received = SimpleNamespace(path=received_path)
    pending = asyncio.get_running_loop().create_future()
    pending.set_result(received)

    @asynccontextmanager
    async def expect_download(**kwargs):
        yield SimpleNamespace(value=pending)

    class Target:
        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def is_enabled(self):
            return True

        async def click(self, **kwargs):
            clicks.append(True)
            redirect.unlink()
            redirect.symlink_to(directory, target_is_directory=True)

    entry = SimpleNamespace(lock=asyncio.Lock(), snapshot_id="observed",
                            page=SimpleNamespace(expect_download=expect_download))
    control = BrowserControl(directory=directory)
    monkeypatch.setattr(control, "_entry", lambda *_: entry)
    monkeypatch.setattr(control, "_check_snapshot", lambda *_: None)
    monkeypatch.setattr(control, "_target_locator", lambda *_: Target())

    async def until_dialog(entry, operation):
        return await operation

    monkeypatch.setattr(control, "_until_dialog", until_dialog)
    args = BrowserDownload(session_id=uuid.uuid4().hex, tab_id=uuid.uuid4().hex,
                           snapshot_id=uuid.uuid4().hex, path=str(redirect / "browser.json"),
                           target=BrowserTarget(selector="#download"))
    with pytest.raises(BrowserActionUnknown) as error:
        await control.download(args, owner="browser-only")
    assert isinstance(error.value.__cause__, ValueError)
    assert "local Chrome selection" in str(error.value.__cause__)
    assert clicks == [True]  # Input happened; its outcome must not be reported as unsubmitted.
    assert not (safe / "browser.json").exists()
    if existing:
        assert saved.read_bytes() == b"existing owner selection"
    else:
        assert not saved.exists()
