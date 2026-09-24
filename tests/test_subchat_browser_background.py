"""Background Chrome launch and owned-tab allocation contracts."""

import sys
from types import SimpleNamespace

import pytest

from anywhere_computer.subchat_browser import background


async def test_background_page_uses_nonactivating_cdp_target():
    page = object()
    context = SimpleNamespace(pages=[])
    commands = []

    class Session:
        async def send(self, method, params):
            commands.append((method, params))
            context.pages.append(page)
            return {'targetId': 'owned-target'}

        async def detach(self):
            commands.append(('detach', None))

    class Browser:
        def is_connected(self):
            return True

        async def new_browser_cdp_session(self):
            return Session()

    context.browser = Browser()
    assert await background.new_background_page(context) is page
    assert commands == [
        ('Target.createTarget', {'url': 'about:blank', 'background': True}),
        ('detach', None),
    ]


@pytest.mark.skipif(sys.platform == 'win32', reason='macOS profile locking')
async def test_background_launch_attaches_only_to_fresh_profile_and_cleans_up(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(background.sys, 'platform', 'darwin')
    monkeypatch.setattr(background, '_profile_processes', lambda _profile: [])
    monkeypatch.setattr(background, '_owned_processes', lambda _profile, _token: [object()])
    stopped = []
    monkeypatch.setattr(
        background, '_stop_profile_processes',
        lambda profile, token, *, startup_grace: stopped.append((profile, token, startup_grace)))
    launched = []

    class Process:
        async def wait(self):
            (tmp_path / 'DevToolsActivePort').write_text('32001\n/devtools/browser/owned\n')
            return 0

    async def launch(*command, **kwargs):
        launched.append(command)
        return Process()

    monkeypatch.setattr(background.asyncio, 'create_subprocess_exec', launch)
    context = object()
    closed = []

    class Browser:
        contexts = [context]

        async def close(self):
            closed.append(True)

    async def connect(endpoint):
        assert endpoint == 'http://127.0.0.1:32001'
        return Browser()

    driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    for _ in range(2):
        async with background.background_chrome_context(driver, tmp_path, []) as attached:
            assert attached is context
        assert not (tmp_path / 'DevToolsActivePort').exists()
    assert '-g' in launched[0] and '-j' in launched[0] and '-n' in launched[0]
    assert '--remote-debugging-port=0' in launched[0]
    assert '--remote-debugging-address=127.0.0.1' in launched[0]
    assert any(arg.startswith('--anywhere-background-owner=') for arg in launched[0])
    assert '--no-startup-window' in launched[0]
    assert closed == [True, True]
    assert len(stopped) == 2 and all(item[0] == tmp_path and item[2] is False
                                     for item in stopped)
    assert stopped[-1][1] == (tmp_path / '.anywhere-background.owner').read_text()


def test_background_cleanup_catches_late_chrome_and_waits_after_kill(tmp_path, monkeypatch):
    class Process:
        def __init__(self):
            self.terminated = False
            self.killed = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    process = Process()
    checks = iter([[], [], [process], []])
    monkeypatch.setattr(background, '_owned_processes',
                        lambda _profile, _token: next(checks))
    monkeypatch.setattr(background.time, 'sleep', lambda _seconds: None)
    waits = iter([([], [process]), ([process], [])])
    monkeypatch.setattr(background.psutil, 'wait_procs',
                        lambda _processes, timeout: next(waits))

    background._stop_profile_processes(tmp_path, 'owner', startup_grace=True)
    assert process.terminated and process.killed


def test_background_cleanup_reports_surviving_chrome(tmp_path, monkeypatch):
    process = SimpleNamespace(terminate=lambda: None, kill=lambda: None)
    monkeypatch.setattr(background, '_owned_processes',
                        lambda _profile, _token: [process])
    monkeypatch.setattr(background.psutil, 'wait_procs',
                        lambda processes, timeout: ([], processes))
    with pytest.raises(RuntimeError, match='did not stop'):
        background._stop_profile_processes(tmp_path, 'owner')


@pytest.mark.skipif(sys.platform == 'win32', reason='macOS profile locking')
async def test_background_launch_rejects_unowned_profile_process(tmp_path, monkeypatch):
    monkeypatch.setattr(background.sys, 'platform', 'darwin')
    monkeypatch.setattr(background, '_profile_processes', lambda _profile: [object()])
    with pytest.raises(ValueError, match='already in use'):
        async with background.background_chrome_context(object(), tmp_path, []):
            pass


@pytest.mark.skipif(sys.platform == 'win32', reason='macOS profile locking')
def test_background_profile_lock_rejects_concurrent_owner(tmp_path):
    with background._profile_lock(tmp_path):
        with pytest.raises(ValueError, match='already in use'):
            with background._profile_lock(tmp_path):
                pass


@pytest.mark.skipif(sys.platform == 'win32', reason='macOS profile locking')
async def test_background_launch_reclaims_owned_orphan_and_stale_port(tmp_path, monkeypatch):
    monkeypatch.setattr(background.sys, 'platform', 'darwin')
    port_file = tmp_path / 'DevToolsActivePort'
    port_file.write_text('32000\n/devtools/browser/stale\n')
    old_token = 'a' * 32
    (tmp_path / '.anywhere-background.owner').write_text(old_token)
    old_process = object()
    monkeypatch.setattr(background, '_profile_processes', lambda _profile: [old_process])

    def owned(_profile, token):
        return [old_process] if token in {old_token, new_token[0]} else []

    new_token = ['']
    monkeypatch.setattr(background, '_owned_processes', owned)
    stopped = []

    def stop(_profile, token, *, startup_grace=False):
        stopped.append((token, startup_grace))

    monkeypatch.setattr(background, '_stop_profile_processes', stop)

    class Process:
        async def wait(self):
            assert not port_file.exists()
            port_file.write_text('32001\n/devtools/browser/new\n')
            return 0

    async def launch(*command, **_kwargs):
        new_token[0] = next(
            arg.removeprefix('--anywhere-background-owner=') for arg in command
            if arg.startswith('--anywhere-background-owner='))
        return Process()

    monkeypatch.setattr(background.asyncio, 'create_subprocess_exec', launch)
    context = object()

    class Browser:
        contexts = [context]

        async def close(self):
            pass

    async def connect(endpoint):
        assert endpoint == 'http://127.0.0.1:32001'
        return Browser()

    driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    async with background.background_chrome_context(driver, tmp_path, []) as attached:
        assert attached is context
    assert stopped == [(old_token, False), (new_token[0], False)]


@pytest.mark.skipif(sys.platform == 'win32', reason='macOS profile locking')
def test_background_owner_token_rejects_symlink(tmp_path):
    owner_file = tmp_path / '.anywhere-background.owner'
    target = tmp_path / 'other-file'
    target.write_text('a' * 32)
    owner_file.symlink_to(target)
    with pytest.raises(OSError):
        background._read_owner_token(owner_file)


async def test_background_input_prepares_model_and_draft_on_offline_chat_fixture():
    playwright = pytest.importorskip('playwright.async_api')
    from test_subchat_browser_backend import HTML

    from anywhere_computer.subchat_browser.backend import INPUT
    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            page = await browser.new_page()
            await page.route('https://chatgpt.com/', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=HTML))
            await page.goto('https://chatgpt.com/')
            menu = page.get_by_role('menu')
            await background.background_pointer_click(
                page.locator('[data-composer-navigation-target="reasoning"]'))
            assert await menu.is_visible()
            await background.background_pointer_click(
                page.get_by_role('menuitemradio', name='Future model'))
            control = page.locator('[data-reasoning-slider="true"]')
            assert await control.is_visible()
            await background.background_key_press(control, 'ArrowLeft')
            assert await page.get_by_role('slider').get_attribute('aria-valuenow') == '0'
            await background.background_key_press(menu, 'Escape')
            assert await menu.is_hidden()
            editor = page.get_by_role('textbox')
            await background.background_focus_editor(editor)
            draft = await page.evaluate(
                INPUT + '\ntext => insertSubchatDraft(document,text)', 'fixture only')
            assert draft == {'state': 'draft_observed', 'input_dispatched': True,
                             'submitted': False}
            assert await page.evaluate('window.sends') == 0
            with pytest.raises(ValueError, match='Unsupported background key'):
                await background.background_key_press(editor, 'Enter')
        finally:
            await browser.close()
