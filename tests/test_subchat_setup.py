import asyncio
import builtins
import json
import os
import stat
import subprocess
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import playwright.async_api
import pytest

from anywhere_computer import subchat_chrome_login, subchat_setup
from anywhere_computer.subchat import SubchatAccessError


def test_inspect_then_select_pins_only_verified_account_without_send(tmp_path, monkeypatch,
                                                                     capsys):
    source = tmp_path / "Chrome" / "Default"
    source.mkdir(parents=True)
    marker = source / "untouched"
    marker.write_text("source data")
    state = tmp_path / "subchat" / "ledger"
    selection = state.parent / "login-selection.json"
    monkeypatch.setattr(subchat_setup, "plugin_paths", lambda: (tmp_path / "login", state))

    async def inspect(observed_source):
        assert observed_source == source
        return "account-a"

    monkeypatch.setattr(subchat_setup, "inspect_account", inspect)
    subchat_setup.main(["inspect", str(source)])
    assert json.loads(capsys.readouterr().out)["account_id"] == "account-a"
    assert not selection.exists()
    with pytest.raises(SystemExit, match="different Chat account"):
        subchat_setup.main(["select", str(source), "--expect-account-id", "account-b"])
    assert not selection.exists()
    subchat_setup.main(["select", str(source), "--expect-account-id", "account-a"])
    output = json.loads(capsys.readouterr().out)
    assert output["selected"] is True and output["background_send_enabled"] is False
    assert json.loads(selection.read_text()) == {
        "chrome_source_profile": str(source), "expected_account_id": "account-a",
        "enable_background_send": False,
    }
    if os.name != "nt":
        assert stat.S_IMODE(selection.stat().st_mode) == 0o600
    assert marker.read_text() == "source data"
    subchat_setup.main(["select", str(source), "--expect-account-id", "account-a",
                        "--enable-background-send"])
    assert json.loads(selection.read_text())["enable_background_send"] is True


def test_setup_authentication_failure_never_saves_selection(tmp_path, monkeypatch):
    source = tmp_path / "Chrome" / "Default"
    source.mkdir(parents=True)
    state = tmp_path / "subchat" / "ledger"
    monkeypatch.setattr(subchat_setup, "plugin_paths", lambda: (tmp_path / "login", state))

    async def expired(_source):
        raise SubchatAccessError(401)

    monkeypatch.setattr(subchat_setup, "inspect_account", expired)
    with pytest.raises(SystemExit, match="Login is missing or expired"):
        subchat_setup.main(["select", str(source), "--expect-account-id", "account-a"])
    assert not (state.parent / "login-selection.json").exists()

    async def unsafe_provider_error(_source):
        raise ValueError("private provider response Bearer secret")

    monkeypatch.setattr(subchat_setup, "inspect_account", unsafe_provider_error)
    with pytest.raises(SystemExit, match="Subchat login inspection failed: ValueError") as error:
        subchat_setup.main(["inspect", str(source)])
    assert "Bearer secret" not in str(error.value)


def test_setup_rejects_non_macos_profile_inspection(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_setup.sys, "platform", "linux")
    with pytest.raises(subchat_setup.SetupInputError, match="macOS only"):
        asyncio.run(subchat_setup.inspect_account(tmp_path / "Default"))


def test_setup_reports_non_macos_without_browser_details(tmp_path, monkeypatch):
    source = tmp_path / "Chrome" / "Default"
    source.mkdir(parents=True)
    state = tmp_path / "subchat" / "ledger"
    monkeypatch.setattr(subchat_setup, "plugin_paths", lambda: (tmp_path / "login", state))
    monkeypatch.setattr(subchat_setup.sys, "platform", "linux")
    with pytest.raises(SystemExit, match="Chrome profile inspection is supported on macOS only"):
        subchat_setup.main(["inspect", str(source)])


def test_setup_reports_missing_browser_extra(tmp_path, monkeypatch):
    source = tmp_path / "Chrome" / "Default"
    source.mkdir(parents=True)
    state = tmp_path / "subchat" / "ledger"
    monkeypatch.setattr(subchat_setup, "plugin_paths", lambda: (tmp_path / "login", state))
    monkeypatch.setattr(subchat_setup.sys, "platform", "darwin")
    original_import = builtins.__import__

    def without_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ModuleNotFoundError("No module named 'playwright'", name="playwright")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_playwright)
    with pytest.raises(SystemExit, match="Install browser support with the browser extra"):
        subchat_setup.main(["inspect", str(source)])


def test_setup_reports_invalid_plugin_paths_without_traceback(tmp_path, monkeypatch):
    source = tmp_path / "Chrome" / "Default"
    source.mkdir(parents=True)

    def invalid_paths():
        raise ValueError("private path detail")

    monkeypatch.setattr(subchat_setup, "plugin_paths", invalid_paths)
    with pytest.raises(SystemExit, match="Subchat setup paths are invalid") as error:
        subchat_setup.main(["inspect", str(source)])
    assert "private path detail" not in str(error.value)


def test_setup_help_imports_without_playwright():
    code = """\
import builtins
import runpy
import sys
original_import = builtins.__import__
def without_playwright(name, *args, **kwargs):
    if name.startswith('playwright'):
        raise ModuleNotFoundError("No module named 'playwright'", name='playwright')
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_playwright
sys.argv = ['anywhere-subchat-setup', '--help']
runpy.run_module('anywhere_computer.subchat_setup', run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            check=False)
    assert result.returncode == 0, result.stderr
    assert "inspect" in result.stdout and "select" in result.stdout


def test_setup_reports_its_own_invalid_profile_without_provider_details(tmp_path, monkeypatch):
    state = tmp_path / "subchat" / "ledger"
    monkeypatch.setattr(subchat_setup, "plugin_paths", lambda: (tmp_path / "login", state))
    with pytest.raises(SystemExit, match="Select an absolute Chrome profile path"):
        subchat_setup.main(["inspect", "relative/Default"])
    other = tmp_path / "Chrome" / "Personal"
    other.mkdir(parents=True)
    with pytest.raises(SystemExit, match="Select Chrome Default or Profile N"):
        subchat_setup.main(["inspect", str(other)])


@pytest.mark.asyncio
async def test_inspection_uses_private_snapshot_and_background_context(tmp_path, monkeypatch):
    source = tmp_path / "Chrome" / "Default"
    snapshot = tmp_path / "private-snapshot"
    context = object()
    observed = []
    monkeypatch.setattr(subchat_setup.sys, "platform", "darwin")

    @asynccontextmanager
    async def fake_playwright():
        observed.append("playwright")
        yield object()

    @asynccontextmanager
    async def fake_snapshot(selected):
        assert selected == source
        observed.append("snapshot")
        yield snapshot

    @asynccontextmanager
    async def fake_background(_driver, profile, launch_args):
        assert profile == snapshot and launch_args == ["--profile-directory=Default"]
        observed.append("background")
        yield context

    @asynccontextmanager
    async def fake_client(*args, **kwargs):
        observed.append("client")
        yield object()

    async def fake_session(actual_context, _client, *, page_factory):
        assert actual_context is context and page_factory is not None
        observed.append("account")
        return SimpleNamespace(account_id="observed-account")

    monkeypatch.setattr(playwright.async_api, "async_playwright", fake_playwright)
    monkeypatch.setattr(subchat_setup, "temporary_chrome_profile", fake_snapshot)
    monkeypatch.setattr(subchat_setup, "background_chrome_context", fake_background)
    monkeypatch.setattr(subchat_setup.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(subchat_chrome_login, "chrome_http_session", fake_session)
    assert await subchat_setup.inspect_account(source) == "observed-account"
    assert observed == ["playwright", "snapshot", "background", "client", "account"]
