import asyncio
import builtins
import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import playwright.async_api
import pytest
from test_subchat_chrome_profile import profile_fixture

from anywhere_computer import subchat_chrome_login, subchat_chrome_profile, subchat_setup
from anywhere_computer.http_service import HTTPServiceConfig, load_http_config
from anywhere_computer.locking import ProcessLock
from anywhere_computer.subchat import SubchatAccessError
from anywhere_computer.subchat_gateway import SubchatGatewayConfig


@pytest.fixture(autouse=True)
def standard_test_chrome_store(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_chrome_profile, "chrome_user_data_root",
                        lambda: tmp_path / "Chrome")


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


def test_windows_dedicated_setup_reports_account_without_enabling_send(
    tmp_path, monkeypatch, capsys,
):
    profile = tmp_path / 'edge-login'
    state = tmp_path / 'ledger'
    monkeypatch.setattr(subchat_setup.sys, 'platform', 'win32')
    monkeypatch.setattr(subchat_setup, 'plugin_paths', lambda: (profile, state))
    monkeypatch.setattr(subchat_setup, 'ProcessLock', lambda _path: nullcontext())
    seen = []

    async def prepare(selected_profile, channel):
        seen.append(('prepare', selected_profile, channel))
        return 'account-a'

    async def inspect(selected_profile, channel):
        seen.append(('inspect', selected_profile, channel))
        return 'account-a'

    monkeypatch.setattr(subchat_setup, 'prepare_dedicated_profile', prepare)
    monkeypatch.setattr(subchat_setup, 'inspect_dedicated_account', inspect)
    subchat_setup.main(['prepare-dedicated', '--browser-channel', 'msedge'])
    assert json.loads(capsys.readouterr().out) == {
        'account_id': 'account-a', 'browser_channel': 'msedge',
        'selected': False, 'send_enabled': False, 'restart_required': False,
    }
    subchat_setup.main(['inspect-dedicated', '--browser-channel', 'msedge'])
    assert json.loads(capsys.readouterr().out)['account_id'] == 'account-a'
    assert seen == [('prepare', profile, 'msedge'), ('inspect', profile, 'msedge')]
    assert not (state.parent / 'login-selection.json').exists()

    with pytest.raises(SystemExit, match='different Chat account'):
        subchat_setup.main(['choose-dedicated', '--browser-channel', 'msedge',
                            '--expect-account-id', 'account-b',
                            '--enable-background-send'])
    assert not (state.parent / 'login-selection.json').exists()

    subchat_setup.main(['choose-dedicated', '--browser-channel', 'msedge',
                        '--expect-account-id', 'account-a', '--enable-background-send'])
    assert json.loads(capsys.readouterr().out)['send_enabled'] is True
    assert json.loads((state.parent / 'login-selection.json').read_text()) == {
        'dedicated_browser_channel': 'msedge', 'dedicated_profile': str(profile),
        'expected_account_id': 'account-a',
        'enable_background_send': True,
    }
    assert seen[-1] == ('inspect', profile, 'msedge')
    subchat_setup.main(['revoke'])
    assert json.loads(capsys.readouterr().out)['selected'] is False
    assert not (state.parent / 'login-selection.json').exists()


async def test_windows_prepare_uses_normal_edge_and_closes_before_account_check(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(subchat_setup.sys, 'platform', 'win32')
    events = []

    def startfile(executable, operation, *, arguments, cwd):
        events.append(('launch', executable, operation, arguments, cwd))

    async def inspector(profile, channel):
        events.append(('inspect', profile, channel))
        return 'account-a'

    monkeypatch.setattr(subchat_setup.os, 'startfile', startfile, raising=False)
    monkeypatch.setattr(subchat_setup, 'inspect_dedicated_account', inspector)
    monkeypatch.setattr(builtins, 'input', lambda _prompt: events.append('owner-enter'))
    profile = tmp_path / 'edge-login'
    assert await subchat_setup.prepare_dedicated_profile(profile, 'msedge') == 'account-a'
    assert events == [
        ('launch', 'msedge.exe', 'open',
         subprocess.list2cmdline([f'--user-data-dir={profile}', 'https://chatgpt.com/']),
         str(profile)),
        'owner-enter', ('inspect', profile, 'msedge'),
    ]


def test_windows_setup_missing_edge_reports_failure_without_selection(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(subchat_setup.sys, 'platform', 'win32')
    state = tmp_path / 'ledger'
    monkeypatch.setattr(subchat_setup, 'plugin_paths',
                        lambda: (tmp_path / 'edge-login', state))

    def startfile(*_args, **_kwargs):
        raise FileNotFoundError('Edge is not installed')

    monkeypatch.setattr(subchat_setup.os, 'startfile', startfile, raising=False)
    monkeypatch.setattr(builtins, 'input',
                        lambda _prompt: pytest.fail('Missing browser must not ask for login'))
    with pytest.raises(SystemExit, match='Subchat login inspection failed: FileNotFoundError'):
        subchat_setup.main(['prepare-dedicated', '--browser-channel', 'msedge'])
    assert not (state.parent / 'login-selection.json').exists()


def test_discover_choose_and_revoke_profile_id(tmp_path, monkeypatch, capsys):
    if os.name == 'nt':
        pytest.skip('Chrome profile ID selection is currently macOS-only')
    root = tmp_path / "Library/Application Support/Google/Chrome"
    for profile_id in ("Default", "Profile 2"):
        (root / profile_id).mkdir(parents=True)
    (root / "Personal").mkdir()
    state = tmp_path / "app/subchat/ledger"
    monkeypatch.setattr(subchat_setup.sys, "platform", "darwin")
    monkeypatch.setattr(subchat_chrome_profile, "chrome_user_data_root", lambda: root)
    monkeypatch.setattr(subchat_setup, "chrome_user_data_root", lambda: root)
    monkeypatch.setattr(subchat_setup, "plugin_paths", lambda: (tmp_path / "login", state))

    async def inspect(source):
        return {"Default": "account-a", "Profile 2": "account-b"}[source.name]

    monkeypatch.setattr(subchat_setup, "inspect_account", inspect)
    subchat_setup.main(["discover"])
    assert json.loads(capsys.readouterr().out) == {"profiles": [
        {"profile_id": "Default", "account_id": "account-a", "state": "available"},
        {"profile_id": "Profile 2", "account_id": "account-b", "state": "available"},
    ]}
    with pytest.raises(SystemExit, match="different Chat account"):
        subchat_setup.main(["choose", "Profile 2", "--expect-account-id", "account-a"])
    assert not (state.parent / "login-selection.json").exists()
    with pytest.raises(SystemExit, match="Select Chrome Default or Profile N"):
        subchat_setup.main(["choose", str(root / "Default"),
                            "--expect-account-id", "account-a"])
    subchat_setup.main(["choose", "Profile 2", "--expect-account-id", "account-b",
                        "--enable-background-send"])
    result = json.loads(capsys.readouterr().out)
    assert result["selected"] is True and result["background_send_enabled"] is True
    record = json.loads((state.parent / "login-selection.json").read_text())
    assert record == {"chrome_profile_id": "Profile 2",
                      "expected_account_id": "account-b", "enable_background_send": True}
    assert stat.S_IMODE((state.parent / "login-selection.json").stat().st_mode) == 0o600
    subchat_setup.main(["revoke"])
    assert json.loads(capsys.readouterr().out)["selected"] is False
    assert not (state.parent / "login-selection.json").exists()


def test_discovery_skips_symlink_and_reports_missing_login(tmp_path, monkeypatch):
    root = tmp_path / "Chrome"
    (root / "Default").mkdir(parents=True)
    (root / "Profile 1").symlink_to(root / "Default")
    (root / "Profile 100").mkdir()
    monkeypatch.setattr(subchat_setup.sys, "platform", "darwin")
    monkeypatch.setattr(subchat_chrome_profile, "chrome_user_data_root", lambda: root)
    monkeypatch.setattr(subchat_setup, "chrome_user_data_root", lambda: root)

    async def expired(_source):
        raise SubchatAccessError(401)

    monkeypatch.setattr(subchat_setup, "inspect_account", expired)
    assert asyncio.run(subchat_setup.discover_profiles()) == [
        {"profile_id": "Default", "state": "login_required"},
        {"profile_id": "Profile 100", "state": "login_required"}]


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


def test_stage_plugin_profile_preserves_pin_and_send_consent(tmp_path, monkeypatch):
    source = profile_fixture(tmp_path / "Chrome", "Default")
    state = tmp_path / "app/subchat/ledger"
    state.parent.mkdir(parents=True)
    selection = state.parent / "login-selection.json"
    selection.write_text(json.dumps({
        "chrome_source_profile": str(source), "expected_account_id": "account-a",
        "enable_background_send": True,
    }))
    monkeypatch.setattr(subchat_setup, "plugin_paths", lambda: (tmp_path / "login", state))

    async def inspect(_source):
        return "account-a"

    monkeypatch.setattr(subchat_setup, "inspect_account", inspect)
    subchat_setup.main(["stage", str(source), "--expect-account-id", "account-a"])
    record = json.loads(selection.read_text())
    staged = Path(record["chrome_source_profile"])
    assert staged.parent.parent == state.parent / "staged-chrome-profiles"
    assert record["expected_account_id"] == "account-a"
    assert record["enable_background_send"] is True
    assert (staged.parent / "Local State").is_file()
    with sqlite3.connect(staged / "Cookies") as database:
        assert database.execute("SELECT COUNT(*) FROM cookies").fetchone()[0] == 2
    with pytest.raises(SystemExit, match="account pin differs"):
        subchat_setup.main(["stage", str(source), "--expect-account-id", "other"])
    assert json.loads(selection.read_text()) == record


def test_stage_http_profile_changes_only_config_profile_under_stopped_service(
    tmp_path, monkeypatch,
):
    source = profile_fixture(tmp_path / "Chrome", "Default")
    directory = tmp_path / "app/chatgpt"
    config_dir = directory / "http-server"
    config_dir.mkdir(parents=True)
    config = HTTPServiceConfig(
        resource="https://example.com/mcp", owner="owner", device="a" * 32,
        client="client", port=8768, scopes=frozenset({"subchat_send"}),
        redirects=frozenset({"https://example.com/callback"}),
        subchat=SubchatGatewayConfig(
            profile=str(source), ledger=str(tmp_path / "ledger"), account_id="account-a",
            consent="ordinary-chat-browser-control-approved"),
    )
    (config_dir / "config.json").write_text(config.model_dump_json())
    authorization = config_dir / "authorization"
    authorization.mkdir()
    (authorization / "keep.txt").write_text("OAuth grants untouched")

    async def inspect(_source):
        return "account-a"

    monkeypatch.setattr(subchat_setup, "inspect_account", inspect)
    command = ["stage", str(source), "--expect-account-id", "account-a",
               "--http-state-dir", str(directory)]
    with ProcessLock(directory / "http-server.lock"):
        with pytest.raises(SystemExit, match="TimeoutError"):
            subchat_setup.main(command)
    assert load_http_config(directory) == config
    subchat_setup.main(command)
    updated = load_http_config(directory)
    assert updated.subchat is not None
    assert updated.subchat.profile != str(source)
    assert updated.subchat.profile.startswith(str(config_dir / "staged-chrome-profiles"))
    assert updated.model_copy(update={"subchat": config.subchat}) == config
    assert (authorization / "keep.txt").read_text() == "OAuth grants untouched"
    first_snapshot = Path(updated.subchat.profile).parent
    subchat_setup.main(command)
    refreshed = load_http_config(directory)
    assert refreshed.subchat is not None
    assert refreshed.subchat.account_id == "account-a"
    assert refreshed.subchat.profile != updated.subchat.profile
    assert not first_snapshot.exists()
    assert (authorization / "keep.txt").read_text() == "OAuth grants untouched"


def test_stage_rejects_symlinked_source(tmp_path, monkeypatch):
    source = profile_fixture(tmp_path / "Chrome", "Default")
    link = tmp_path / "linked"
    link.symlink_to(source)
    monkeypatch.setattr(subchat_setup, "plugin_paths", lambda: (tmp_path / "login",
                                                                tmp_path / "app/ledger"))
    with pytest.raises(SystemExit, match="symbolic links"):
        subchat_setup.main(["stage", str(link), "--expect-account-id", "account-a"])


@pytest.mark.asyncio
async def test_stage_rejects_symlinked_destination_parent(tmp_path):
    source = tmp_path / "Chrome" / "Default"
    source.mkdir(parents=True)
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(subchat_setup.SetupInputError, match="symlink"):
        await subchat_setup.stage_profile(source, linked / "snapshots", "account-a")
