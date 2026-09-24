"""Background Chrome launch and owned-tab allocation contracts."""

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


async def test_background_launch_attaches_only_to_fresh_profile_and_cleans_up(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(background.sys, 'platform', 'darwin')
    process_checks = iter([[], [object()]])
    monkeypatch.setattr(background, '_profile_processes', lambda _profile: next(process_checks))
    stopped = []
    monkeypatch.setattr(
        background, '_stop_profile_processes',
        lambda profile, *, startup_grace: stopped.append((profile, startup_grace)))
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
    async with background.background_chrome_context(driver, tmp_path, []) as attached:
        assert attached is context
    assert '-g' in launched[0] and '-j' in launched[0] and '-n' in launched[0]
    assert '--remote-debugging-port=0' in launched[0]
    assert '--remote-debugging-address=127.0.0.1' in launched[0]
    assert '--no-startup-window' in launched[0]
    assert closed == [True] and stopped == [(tmp_path, False)]


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
    monkeypatch.setattr(background, '_profile_processes', lambda _profile: next(checks))
    monkeypatch.setattr(background.time, 'sleep', lambda _seconds: None)
    waits = iter([([], [process]), ([process], [])])
    monkeypatch.setattr(background.psutil, 'wait_procs',
                        lambda _processes, timeout: next(waits))

    background._stop_profile_processes(tmp_path, startup_grace=True)
    assert process.terminated and process.killed


def test_background_cleanup_reports_surviving_chrome(tmp_path, monkeypatch):
    process = SimpleNamespace(terminate=lambda: None, kill=lambda: None)
    monkeypatch.setattr(background, '_profile_processes', lambda _profile: [process])
    monkeypatch.setattr(background.psutil, 'wait_procs',
                        lambda processes, timeout: ([], processes))
    with pytest.raises(RuntimeError, match='did not stop'):
        background._stop_profile_processes(tmp_path)


async def test_background_launch_rejects_existing_cdp_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(background.sys, 'platform', 'darwin')
    (tmp_path / 'DevToolsActivePort').write_text('32001\n/devtools/browser/other\n')
    with pytest.raises(ValueError, match='already in use'):
        async with background.background_chrome_context(object(), tmp_path, []):
            pass


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
