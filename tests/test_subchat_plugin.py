import io
import json
import os
from pathlib import Path

import pytest
from test_subchat_lifecycle import BrowserFixture

from anywhere_computer import subchat_chrome_profile, subchat_plugin
from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_mcp import READ_ONLY_TOOLS, session
from anywhere_computer.subchat_state import SubchatSubmissions


@pytest.fixture(autouse=True)
def standard_test_chrome_store(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_chrome_profile, 'chrome_user_data_root',
                        lambda: tmp_path / 'Chrome')


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
    monkeypatch.delenv('ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT', raising=False)
    monkeypatch.setattr(subchat_plugin, 'plugin_paths', lambda: (profile, state))
    observed = {}

    async def fake_run(browser_profile: Path | None, state_dir: Path, **options: object) -> None:
        observed.update(profile=browser_profile, state=state_dir, options=options)

    monkeypatch.setattr(subchat_plugin, 'run', fake_run)
    subchat_plugin.main()
    assert observed == {
        'profile': None, 'state': state,
        'options': {'mcp': True, 'http_only': True, 'chrome_login_profile': profile,
                    'chrome_login_source_profile': None,
                    'expected_account_id': None,
                    'read_only_mcp': True},
    }


def test_plugin_source_profile_is_explicit_and_browser_send_stays_dedicated(
    tmp_path, monkeypatch,
):
    profile, state = tmp_path / 'login', tmp_path / 'ledger'
    source = tmp_path / 'Chrome/Default'
    source.mkdir(parents=True)
    monkeypatch.setattr(subchat_plugin, 'plugin_paths', lambda: (profile, state))
    monkeypatch.setenv('ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE', str(source))
    observed = {}

    async def fake_run(browser_profile: Path | None, state_dir: Path, **options: object) -> None:
        observed.update(profile=browser_profile, state=state_dir, options=options)

    monkeypatch.setattr(subchat_plugin, 'run', fake_run)
    subchat_plugin.main()
    assert observed['options']['chrome_login_profile'] is None
    assert observed['options']['chrome_login_source_profile'] == source
    monkeypatch.setenv('ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT', 'browser-send')
    subchat_plugin.main()
    assert observed['profile'] == profile
    assert observed['options'] == {'mcp': True, 'http_read': True, 'minimized': True}


def test_plugin_reads_persistent_local_chrome_selection(tmp_path, monkeypatch):
    profile, state = tmp_path / 'login', tmp_path / 'subchat/ledger'
    source = tmp_path / 'Chrome/Default'
    source.mkdir(parents=True)
    state.parent.mkdir()
    (state.parent / 'login-selection.json').write_text(json.dumps({
        'chrome_source_profile': str(source),
        'expected_account_id': 'pinned-account',
    }))
    monkeypatch.delenv('ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE', raising=False)
    monkeypatch.setattr(subchat_plugin, 'plugin_paths', lambda: (profile, state))
    monkeypatch.setattr(subchat_plugin.sys, 'platform', 'linux')
    observed = {}

    async def fake_run(_browser_profile, _state_dir, **options):
        observed.update(options)

    monkeypatch.setattr(subchat_plugin, 'run', fake_run)
    subchat_plugin.main()
    assert observed['chrome_login_source_profile'] == source
    assert observed['chrome_login_profile'] is None
    assert observed['expected_account_id'] == 'pinned-account'
    monkeypatch.setenv('ANYWHERE_SUBCHAT_EXPECTED_ACCOUNT_ID', 'override-account')
    subchat_plugin.main()
    assert observed['expected_account_id'] == 'override-account'
    monkeypatch.delenv('ANYWHERE_SUBCHAT_EXPECTED_ACCOUNT_ID')
    (state.parent / 'login-selection.json').write_text(json.dumps({
        'chrome_source_profile': 'relative/Default',
    }))
    with pytest.raises(ValueError, match='absolute path'):
        subchat_plugin.main()
    (state.parent / 'login-selection.json').write_text(json.dumps({
        'chrome_source_profile': str(source), 'expected_account_id': '',
    }))
    with pytest.raises(ValueError, match='nonempty account ID'):
        subchat_plugin.main()


def test_profile_id_selection_resolves_only_ordinary_chrome_store(tmp_path, monkeypatch):
    if os.name == 'nt':
        pytest.skip('Chrome profile ID selection is currently macOS-only')
    root = tmp_path / 'Library/Application Support/Google/Chrome'
    source = root / 'Profile 2'
    source.mkdir(parents=True)
    state = tmp_path / 'subchat/ledger'
    state.parent.mkdir()
    selection = state.parent / 'login-selection.json'
    selection.write_text(json.dumps({
        'chrome_profile_id': 'Profile 2', 'expected_account_id': 'account-b',
        'enable_background_send': True,
    }))
    selection.chmod(0o600)
    monkeypatch.setattr(subchat_chrome_profile, 'chrome_user_data_root', lambda: root)
    monkeypatch.delenv('ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE', raising=False)
    monkeypatch.delenv('ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT', raising=False)
    assert subchat_plugin.selected_chrome_login(state) == (source, 'account-b')
    monkeypatch.setenv('ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE', str(tmp_path / 'other/Default'))
    with pytest.raises(ValueError, match='cannot override a selected Chrome ID'):
        subchat_plugin.selected_chrome_login(state)
    monkeypatch.delenv('ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE')
    selection.write_text(json.dumps({
        'chrome_profile_id': '../Default', 'expected_account_id': 'account-b',
    }))
    with pytest.raises(ValueError, match='Select Chrome Default or Profile N'):
        subchat_plugin.selected_chrome_login(state)
    selection.write_text(json.dumps({
        'chrome_profile_id': 'Profile 2',
        'chrome_source_profile': str(source),
    }))
    with pytest.raises(ValueError, match='one Chrome profile'):
        subchat_plugin.selected_chrome_login(state)
    selection.write_text(json.dumps({
        'chrome_profile_id': 'Profile 2', 'expected_account_id': 'account-b',
    }))
    if os.name != 'nt':
        selection.chmod(0o644)
        with pytest.raises(ValueError, match='not private'):
            subchat_plugin.selected_chrome_login(state)


def test_legacy_profile_path_must_be_standard_or_managed(tmp_path, monkeypatch):
    monkeypatch.setattr(subchat_plugin.sys, 'platform', 'darwin')
    state = tmp_path / 'subchat/ledger'
    state.parent.mkdir()
    selection = state.parent / 'login-selection.json'
    outside = tmp_path / 'other/Default'
    outside.mkdir(parents=True)
    selection.write_text(json.dumps({
        'chrome_source_profile': str(outside), 'expected_account_id': 'account-a',
    }))
    with pytest.raises(ValueError, match='outside the managed store'):
        subchat_plugin.selected_chrome_login(state)
    staged = state.parent / 'staged-chrome-profiles/snapshot-123/Default'
    staged.mkdir(parents=True)
    selection.write_text(json.dumps({
        'chrome_source_profile': str(staged), 'expected_account_id': 'account-a',
    }))
    assert subchat_plugin.selected_chrome_login(state) == (staged, 'account-a')


def test_pinned_macos_login_enables_background_httpx_send_by_default(
    tmp_path, monkeypatch,
):
    profile, state = tmp_path / 'login', tmp_path / 'subchat/ledger'
    source = tmp_path / 'Chrome/Default'
    source.mkdir(parents=True)
    state.parent.mkdir()
    (state.parent / 'login-selection.json').write_text(json.dumps({
        'chrome_source_profile': str(source),
        'expected_account_id': 'pinned-account',
        'enable_background_send': True,
    }))
    monkeypatch.delenv('ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT', raising=False)
    monkeypatch.setattr(subchat_plugin, 'plugin_paths', lambda: (profile, state))
    monkeypatch.setattr(subchat_plugin.sys, 'platform', 'darwin')
    observed = {}

    async def fake_run(browser_profile, state_dir, **options):
        observed.update(profile=browser_profile, state=state_dir, options=options)

    monkeypatch.setattr(subchat_plugin, 'run', fake_run)
    subchat_plugin.main()
    assert observed == {
        'profile': profile, 'state': state,
        'options': {
            'mcp': True, 'http_read': True, 'minimized': True,
            'httpx_generation': True, 'browser_source_profile': source,
            'expected_account_id': 'pinned-account',
        },
    }


def test_pin_without_explicit_send_selection_remains_read_only(tmp_path, monkeypatch):
    profile, state = tmp_path / 'login', tmp_path / 'subchat/ledger'
    source = tmp_path / 'Chrome/Default'
    source.mkdir(parents=True)
    state.parent.mkdir()
    selection = state.parent / 'login-selection.json'
    selection.write_text(json.dumps({
        'chrome_source_profile': str(source), 'expected_account_id': 'pinned-account',
    }))
    monkeypatch.delenv('ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT', raising=False)
    monkeypatch.setattr(subchat_plugin, 'plugin_paths', lambda: (profile, state))
    monkeypatch.setattr(subchat_plugin.sys, 'platform', 'darwin')
    observed = {}

    async def fake_run(browser_profile, _state_dir, **options):
        observed.update(profile=browser_profile, options=options)

    monkeypatch.setattr(subchat_plugin, 'run', fake_run)
    subchat_plugin.main()
    assert observed['profile'] is None
    assert observed['options']['read_only_mcp'] is True
    monkeypatch.setenv('ANYWHERE_SUBCHAT_EXPECTED_ACCOUNT_ID', 'other-account')
    subchat_plugin.main()
    assert observed['options']['read_only_mcp'] is True


def test_plugin_selection_stat_error_is_sanitized(tmp_path, monkeypatch):
    selection = tmp_path / 'login-selection.json'
    original_stat = Path.lstat

    def denied_stat(path, *args, **kwargs):
        if path == selection:
            raise PermissionError('private selection path')
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', denied_stat)
    with pytest.raises(ValueError, match='Invalid Subchat login selection') as error:
        subchat_plugin.selected_chrome_login(tmp_path / 'ledger')
    assert 'private selection path' not in str(error.value)


def test_plugin_browser_send_opt_in_reuses_login_with_minimized_window(tmp_path, monkeypatch):
    login, state = tmp_path / 'login', tmp_path / 'ledger'
    browser = tmp_path / 'browser-send'
    monkeypatch.setattr(subchat_plugin, 'state_directory', lambda: tmp_path)
    monkeypatch.setattr(subchat_plugin, 'plugin_paths', lambda: (login, state))
    monkeypatch.setenv('ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT', 'browser-send')
    monkeypatch.delenv('ANYWHERE_SUBCHAT_BROWSER_SEND_PROFILE', raising=False)
    observed = {}

    async def fake_run(browser_profile: Path | None, state_dir: Path, **options: object) -> None:
        observed.update(profile=browser_profile, state=state_dir, options=options)

    monkeypatch.setattr(subchat_plugin, 'run', fake_run)
    subchat_plugin.main()
    assert observed == {
        'profile': login, 'state': state,
        'options': {'mcp': True, 'http_read': True, 'minimized': True},
    }
    monkeypatch.setenv('ANYWHERE_SUBCHAT_BROWSER_SEND_PROFILE', str(browser))
    subchat_plugin.main()
    assert observed['profile'] == browser
    monkeypatch.setenv('ANYWHERE_SUBCHAT_BROWSER_SEND_PROFILE', str(state / 'child'))
    with pytest.raises(ValueError, match='separate from state'):
        subchat_plugin.main()
    monkeypatch.setenv('ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT', 'typo')
    with pytest.raises(ValueError, match='must be http-read-only or browser-send'):
        subchat_plugin.main()


def test_plugin_httpx_mode_keeps_minimized_fallback_on_other_platforms(tmp_path, monkeypatch):
    profile, state = tmp_path / 'profile', tmp_path / 'ledger'
    monkeypatch.setattr(subchat_plugin, 'plugin_paths', lambda: (profile, state))
    monkeypatch.setenv('ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT', 'browser-prepared-httpx')
    observed = {}

    async def fake_run(_profile, _state, **options):
        observed.update(options)

    monkeypatch.setattr(subchat_plugin, 'run', fake_run)
    subchat_plugin.main()
    assert observed['httpx_generation'] is True
    assert observed['minimized'] is True


async def test_plugin_browser_send_mode_exposes_and_dispatches_send(tmp_path, monkeypatch):
    from anywhere_computer import mcp_server, subchat_cli
    from anywhere_computer.subchat_browser import backend as browser_backend

    backend = BrowserFixture()

    async def ui_catalog(_model):
        return {'state': 'catalog_observed', 'models': []}

    async def http_catalog():
        return {'state': 'http_catalog_observed', 'versions': []}

    backend.catalog = ui_catalog
    backend.http_catalog = http_catalog
    backend.capabilities = lambda: {
        'generation_transport': 'browser_prepared', 'browser_required': True,
        'http_selection_send_supported': True,
    }
    monkeypatch.setattr(browser_backend, 'BrowserSubchatBackend',
                        lambda *_args, **_kwargs: backend)
    observed = {}

    async def inspect(server, _source, _destination):
        observed['tools'] = {tool['name'] for tool in await server.catalog()}
        capability = await server.execute(Request(operation_id='a' * 32,
                                                  tool='subchat_capabilities', arguments={}))
        observed['transport'] = capability.data['generation_transport']
        result = await server.execute(Request(operation_id='b' * 32,
                                              tool='subchat_send', arguments={
                                                  'prompt': 'hello', 'model': 'observed model',
                                                  'effort': 'observed effort'}))
        observed['result'] = result

    monkeypatch.setattr(mcp_server, 'serve_stdio', inspect)
    await subchat_cli.run(tmp_path / 'browser-send', tmp_path / 'ledger', mcp=True,
                          http_read=True, minimized=True)
    assert 'subchat_send' in observed['tools']
    assert observed['transport'] == 'browser_prepared'
    assert observed['result'].state == 'unknown'
    assert backend.sends == 1


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
            READ_ONLY_TOOLS - {'subchat_capabilities', 'subchat_catalog',
                               'subchat_download_file', 'subchat_download_image',
                               'subchat_refresh_auth'})
        for name in ('subchat_send', 'subchat_delete', 'subchat_message',
                     'subchat_cancel', 'subchat_queue_watch'):
            result = await server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                          'params': {'name': name, 'arguments': {}}})
            assert result is not None
            assert result['error']['message'] == 'Unknown tool'
    finally:
        await server.close()
        ledger.close()


async def test_plugin_http_catalog_defaults_to_http_and_missing_id_is_clear(tmp_path):
    ledger = Ledger(tmp_path)

    async def http_catalog():
        return {'state': 'http_catalog_observed', 'versions': []}

    server = session(Subchats(SubchatSubmissions(ledger.connection), BrowserFixture()),
                     observe_http_catalog=http_catalog,
                     read_only=True)
    try:
        definitions = {item['name']: item for item in await server.catalog()}
        assert definitions['subchat_catalog']['inputSchema']['properties']['source'][
            'default'] == 'http'
        assert definitions['subchat_catalog']['annotations']['readOnlyHint'] is True
        catalog = await server.execute(Request(operation_id='a' * 32,
                                               tool='subchat_catalog', arguments={}))
        assert catalog.state == 'completed'
        assert catalog.data['state'] == 'http_catalog_observed'
        missing = await server.execute(Request(operation_id='b' * 32,
                                               tool='subchat_status',
                                               arguments={'operation_id': 'c' * 32}))
        assert missing.state == 'failed'
        assert missing.data['error_code'] == 'unknown_operation'
        assert 'subchat_list' in (missing.error or '')
    finally:
        await server.close()
        ledger.close()


async def test_cli_unknown_operation_names_the_ledger_check(tmp_path):
    from anywhere_computer.subchat_cli import process_lines

    ledger = Ledger(tmp_path)
    source = io.StringIO(json.dumps({'action': 'status', 'operation_id': 'c' * 32}) + '\n')
    output = io.StringIO()
    try:
        await process_lines(Subchats(SubchatSubmissions(ledger.connection), BrowserFixture()),
                            source, output)
        result = json.loads(output.getvalue())
        assert result['state'] == 'unknown_operation'
        assert 'ledger' in result['next_action']
    finally:
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
    assert observed['tools'] == READ_ONLY_TOOLS - {'subchat_refresh_auth'}
    assert 'generation_transport=unavailable' in observed['instructions']


async def test_plugin_explicit_refresh_only_reauthenticates_selected_read_session(tmp_path):
    from test_subchat_http_history import sample
    from test_subchat_http_only import Client, credentials

    from anywhere_computer.models import Request
    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    ledger = Ledger(tmp_path)
    _, payload = sample()
    client = Client(payload)
    refreshed = []

    async def factory():
        return client

    async def refresh(account_id):
        refreshed.append(account_id)
        return credentials(), factory

    backend = HTTPOnlySubchatBackend(factory, credentials(), chrome_login=True,
                                     refresh_session=refresh)
    server = session(Subchats(SubchatSubmissions(ledger.connection), backend),
                     observe_http_catalog=backend.http_catalog, read_only=True)
    try:
        definitions = {item['name']: item for item in await server.catalog()}
        assert set(definitions) == READ_ONLY_TOOLS
        assert definitions['subchat_refresh_auth']['annotations']['readOnlyHint'] is True
        response = await server.execute(Request(operation_id='a' * 32,
                                                tool='subchat_refresh_auth', arguments={}))
        assert response.state == 'completed'
        assert response.data == {
            'authentication_state': 'authenticated',
            'generation_transport': 'unavailable', 'credential_refresh': True}
        assert refreshed == ['fixture-account']
        assert client.calls == []
        count = ledger.connection.execute(
            'SELECT COUNT(*) FROM subchat_submissions').fetchone()[0]
        assert count == 0
    finally:
        await server.close()
        ledger.close()


async def test_plugin_capabilities_describe_exposed_read_only_tools(tmp_path):
    from test_subchat_http_only import credentials

    from anywhere_computer import __version__
    from anywhere_computer.models import Request
    from anywhere_computer.runtime_identity import runtime_identity
    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    ledger = Ledger(tmp_path)

    async def unused_client():
        raise AssertionError('Capabilities must not make an HTTP request')

    backend = HTTPOnlySubchatBackend(unused_client, credentials())
    server = session(Subchats(SubchatSubmissions(ledger.connection), backend),
                     read_only=True)
    try:
        assert backend.capabilities()['http_delete_supported'] is True
        reply = await server.execute(Request(operation_id='d' * 32,
                                             tool='subchat_capabilities', arguments={}))
        assert reply.state == 'completed'
        assert reply.data['http_delete_supported'] is False
        assert reply.data['deletion_transport'] == 'unavailable'
        assert reply.data['generation_transport'] == 'unavailable'
        assert reply.data['http_selection_send_supported'] is False
        assert reply.data['queue_dispatch'] == 'unavailable'
        assert reply.data['queue_watch_supported'] is False
        assert reply.data['implementation_version'] == __version__
        assert reply.data['implementation_runtime_id'] == runtime_identity()
        assert 'subchat_delete' not in {tool['name'] for tool in await server.catalog()}
    finally:
        await server.close()
        ledger.close()


async def test_read_only_recovery_cannot_dispatch_saved_queued_followup(tmp_path):
    from anywhere_computer.models import Request

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    backend = BrowserFixture()
    service = Subchats(store, backend)
    parent, child = 'a' * 32, 'b' * 32
    store.prepare(parent, 'input', 'model', 'effort', owner=None)
    store.begin_send(parent, owner=None)
    store.submitted(parent, 'chat', 'input-message', owner=None)
    store.complete(parent, 'answer-message', 'done', owner=None)
    service.queue(child, parent, 'follow-up', owner=None)
    server = session(service, read_only=True)
    try:
        for tool, arguments in (
            ('subchat_recover', {'operation_id': child}),
            ('subchat_wait', {'operation_id': child, 'wait_ms': 10}),
        ):
            reply = await server.execute(Request(operation_id='c' * 32, tool=tool,
                                                 arguments=arguments))
            assert reply.state == 'completed'
            assert reply.data['state'] == 'queued'
            assert store.get(child, owner=None).state == 'queued'
            assert backend.sends == 0
    finally:
        await server.close()
        ledger.close()


@pytest.mark.parametrize('pinned_account_id', [None, 'fixture-account'])
async def test_chrome_login_closes_before_plugin_serves_tools(
        tmp_path, monkeypatch, pinned_account_id):
    import playwright.async_api
    from test_subchat_http_only import credentials

    from anywhere_computer import mcp_server, subchat_chrome_login, subchat_cli

    events = []

    class Context:
        async def close(self):
            events.append('chrome_closed')

    class Chromium:
        async def launch_persistent_context(self, profile, *, channel, headless,
                                            ignore_default_args, args):
            assert profile == str(tmp_path / 'login')
            assert channel == 'chrome' and headless is True
            assert ignore_default_args == ['--use-mock-keychain']
            assert args == []
            events.append('chrome_started')
            return Context()

    class Driver:
        chromium = Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            events.append('driver_closed')

    async def auth(_context, _client, *, expected_account_id):
        assert expected_account_id == pinned_account_id
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
                          chrome_login_profile=tmp_path / 'login', read_only_mcp=True,
                          expected_account_id=pinned_account_id)
    assert events[-1] == 'driver_closed'
