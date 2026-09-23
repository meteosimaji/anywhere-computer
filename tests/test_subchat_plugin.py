from pathlib import Path

import pytest
from test_subchat_lifecycle import BrowserFixture

from anywhere_computer import subchat_plugin
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_mcp import READ_ONLY_TOOLS, session
from anywhere_computer.subchat_state import SubchatSubmissions


def test_plugin_paths_use_a_dedicated_default(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_plugin, 'state_directory', lambda: tmp_path)
    assert subchat_plugin.plugin_paths() == (
        tmp_path / 'subchat/chrome-login', tmp_path / 'subchat/ledger')


def test_plugin_paths_allow_absolute_overrides_and_reject_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_plugin, 'state_directory', lambda: tmp_path)
    profile = tmp_path / 'separate-chrome-login'
    state = tmp_path / 'separate-ledger'
    monkeypatch.setenv('ANYWHERE_SUBCHAT_CHROME_LOGIN_PROFILE', str(profile))
    monkeypatch.setenv('ANYWHERE_SUBCHAT_STATE_DIR', str(state))
    assert subchat_plugin.plugin_paths() == (profile, state)
    monkeypatch.setenv('ANYWHERE_SUBCHAT_STATE_DIR', 'relative-ledger')
    with pytest.raises(ValueError, match='absolute path'):
        subchat_plugin.plugin_paths()
    monkeypatch.setenv('ANYWHERE_SUBCHAT_STATE_DIR', str(profile / 'ledger'))
    with pytest.raises(ValueError, match='must be separate'):
        subchat_plugin.plugin_paths()


def test_plugin_entry_uses_headless_http_read_only_mode(tmp_path, monkeypatch):
    profile, state = tmp_path / 'login', tmp_path / 'ledger'
    monkeypatch.setattr(subchat_plugin, 'plugin_paths', lambda: (profile, state))
    observed = {}

    async def fake_run(browser_profile: Path | None, state_dir: Path, **options: object) -> None:
        observed.update(profile=browser_profile, state=state_dir, options=options)

    monkeypatch.setattr(subchat_plugin, 'run', fake_run)
    subchat_plugin.main()
    assert observed == {
        'profile': None, 'state': state,
        'options': {'mcp': True, 'http_only': True, 'chrome_login_profile': profile,
                    'read_only_mcp': True},
    }


async def test_plugin_read_only_catalog_rejects_mutations(tmp_path):
    ledger = Ledger(tmp_path)
    server = session(Subchats(SubchatSubmissions(ledger.connection), BrowserFixture()),
                     read_only=True)
    try:
        await server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                             'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                                        'clientInfo': {'name': 'test', 'version': '1'}}})
        await server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        listing = await server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list',
                                       'params': {}})
        assert listing is not None
        assert {tool['name'] for tool in listing['result']['tools']} == (
            READ_ONLY_TOOLS - {'subchat_capabilities', 'subchat_catalog'})
        for name in ('subchat_send', 'subchat_delete', 'subchat_message',
                     'subchat_cancel', 'subchat_queue_watch'):
            result = await server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                          'params': {'name': name, 'arguments': {}}})
            assert result is not None
            assert result['error']['message'] == 'Unknown tool'
    finally:
        await server.close()
        ledger.close()


async def test_cli_read_only_mode_wires_filtered_stdio_session(tmp_path, monkeypatch):
    from test_subchat_http_only import credentials

    from anywhere_computer import mcp_server, subchat_cli

    observed = {}

    async def inspect(server, _source, _destination):
        observed['tools'] = {tool['name'] for tool in await server.catalog()}
        observed['instructions'] = server.instructions

    monkeypatch.setattr(mcp_server, 'serve_stdio', inspect)
    await subchat_cli.run(None, tmp_path, http_only=True, http_session=credentials(),
                          mcp=True, read_only_mcp=True)
    assert observed['tools'] == READ_ONLY_TOOLS
    assert 'generation_transport=unavailable' in observed['instructions']


async def test_chrome_login_closes_before_plugin_serves_tools(tmp_path, monkeypatch):
    import playwright.async_api
    from test_subchat_http_only import credentials

    from anywhere_computer import mcp_server, subchat_chrome_login, subchat_cli

    events = []

    class Context:
        async def close(self):
            events.append('chrome_closed')

    class Chromium:
        async def launch_persistent_context(self, profile, *, channel, headless):
            assert profile == str(tmp_path / 'login')
            assert channel == 'chrome' and headless is True
            events.append('chrome_started')
            return Context()

    class Driver:
        chromium = Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            events.append('driver_closed')

    async def auth(_context, _client):
        events.append('http_authenticated')
        return credentials()

    async def serve(_server, _source, _destination):
        events.append('mcp_serving')
        assert events[:4] == [
            'chrome_started', 'http_authenticated', 'chrome_closed', 'mcp_serving']

    monkeypatch.setattr(playwright.async_api, 'async_playwright', Driver)
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', auth)
    monkeypatch.setattr(mcp_server, 'serve_stdio', serve)
    await subchat_cli.run(None, tmp_path / 'ledger', http_only=True, mcp=True,
                          chrome_login_profile=tmp_path / 'login', read_only_mcp=True)
    assert events[-1] == 'driver_closed'
