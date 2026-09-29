"""Temporary reads retain results, bounded cleanup and exact original-tab isolation."""
import asyncio
import json

import pytest

from anywhere_computer.subchat_browser import http_reader


class ReadResponse:
    @property
    def request(self):
        return self

    async def header_value(self, name):
        return 'Bearer private-fixture' if name == 'authorization' else None

    async def body(self):
        return b'{"ok":true}'


class CleanupPage:
    def __init__(self, mode='normal', retry='normal'):
        self.mode, self.retry = mode, retry
        self.close_calls = 0
        self.closed = False
        self.listeners = []
        self.commands = []
        self.detached = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.context = self

    async def new_page(self):
        return self

    def is_closed(self):
        return self.closed

    def on(self, event, callback):
        assert event == 'close'
        self.listeners.append(callback)

    def remove_listener(self, event, callback):
        assert event == 'close'
        self.listeners.remove(callback)

    def finish(self):
        self.closed = True
        for callback in self.listeners:
            callback(self)

    async def close(self):
        self.close_calls += 1
        self.started.set()
        if self.mode == 'timeout':
            await asyncio.Future()
        if self.mode == 'error':
            raise RuntimeError('private-fixture content in driver error')
        if self.mode == 'delayed':
            await self.release.wait()
        self.finish()

    async def new_cdp_session(self, page):
        assert page is self
        if self.retry == 'attach_timeout':
            await asyncio.Future()
        if self.retry == 'attach_after_close_timeout' and self.started.is_set():
            await asyncio.Future()
        return self

    async def send(self, command, params=None):
        self.commands.append((command, params))
        if command == 'Target.getTargetInfo':
            return {'targetInfo': {'type': 'page', 'targetId': 'owned-target'}}
        assert command == 'Target.closeTarget'
        assert params == {'targetId': 'owned-target'}
        if self.retry == 'error':
            raise RuntimeError('private-fixture content in driver error')
        if self.retry in ('normal', 'attach_after_close_timeout'):
            self.finish()
        # A positive acknowledgement is not proof that the target went away.
        return {'success': True}

    async def detach(self):
        self.detached = True
        if self.retry == 'detach_timeout':
            await asyncio.Future()


@pytest.fixture
def short_cleanup(monkeypatch):
    monkeypatch.setattr(http_reader, '_PAGE_CLOSE_TIMEOUT', .02)
    monkeypatch.setattr(http_reader, '_PAGE_CLOSE_RETRY_TIMEOUT', .03)
    monkeypatch.setattr(http_reader, '_CDP_DETACH_TIMEOUT', .02)


@pytest.mark.parametrize('observer_fails', [False, True])
@pytest.mark.parametrize('mode', ['normal', 'timeout', 'error'])
async def test_bootstrap_cleanup_preserves_result_and_waits_for_retry(
        short_cleanup, observer_fails, mode, caplog):
    page = CleanupPage(mode)
    reader = http_reader.ChatHTTPReader()

    async def observe(selected):
        assert selected is page
        if observer_fails:
            raise ValueError('observation failed')
        return ReadResponse()

    if observer_fails:
        with pytest.raises(ValueError, match='observation failed'):
            await reader._read(page, None, observe)
    else:
        assert await reader._read(page, None, observe) == b'{"ok":true}'
    assert page.closed
    assert page.close_calls == 1
    assert not reader._pending_closes
    assert not page.listeners
    assert page.commands[0] == ('Target.getTargetInfo', None)
    assert any(command == 'Target.closeTarget' for command, _ in page.commands) == (
        mode != 'normal')
    assert 'private-fixture' not in caplog.text


@pytest.mark.parametrize('retry', ['attach_timeout', 'error', 'no_close', 'detach_timeout'])
async def test_unresponsive_cleanup_is_bounded_and_does_not_mask_result(
        short_cleanup, retry, caplog):
    page = CleanupPage('timeout', retry)
    reader = http_reader.ChatHTTPReader()

    async def observe(_):
        return ReadResponse()

    assert await asyncio.wait_for(reader._read(page, None, observe), .5) == b'{"ok":true}'
    assert not page.closed
    assert not reader._pending_closes
    assert not page.listeners
    assert page.detached == (retry != 'attach_timeout')
    assert 'could not be closed' in caplog.text
    assert 'private-fixture' not in caplog.text


async def test_retry_uses_target_session_prepared_before_close(short_cleanup):
    page = CleanupPage('timeout', 'attach_after_close_timeout')
    reader = http_reader.ChatHTTPReader()

    async def observe(_):
        return ReadResponse()

    assert await asyncio.wait_for(reader._read(page, None, observe), .5) == b'{"ok":true}'
    assert page.closed
    assert page.commands == [
        ('Target.getTargetInfo', None),
        ('Target.closeTarget', {'targetId': 'owned-target'}),
    ]
    assert not reader._pending_closes


async def test_direct_page_close_does_not_delay_for_target_preparation(short_cleanup):
    page = CleanupPage('normal', 'attach_timeout')
    await http_reader.ChatHTTPReader().close_owned_page(page)
    assert page.closed
    assert page.commands == []


@pytest.mark.parametrize('during_cleanup', [False, True])
async def test_caller_cancellation_retains_bounded_cleanup(short_cleanup, during_cleanup):
    page = CleanupPage('delayed')
    reader = http_reader.ChatHTTPReader()
    observed = asyncio.Event()

    async def observe(_):
        observed.set()
        if not during_cleanup:
            await asyncio.Future()
        return ReadResponse()

    task = asyncio.create_task(reader._read(page, None, observe))
    await asyncio.wait_for(page.started.wait() if during_cleanup else observed.wait(), .5)
    task.cancel()
    page.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    if reader._pending_closes:
        await asyncio.wait_for(asyncio.gather(*reader._pending_closes), .5)
    await asyncio.sleep(0)  # Run the completion callbacks of an already-finished task.
    assert page.closed and not reader._pending_closes


@pytest.mark.parametrize('observer_fails', [False, True])
async def test_chromium_retry_closes_only_owned_page_before_read_returns(
        monkeypatch, observer_fails):
    """Inject an unresponsive close; run the fallback against real Chrome targets."""
    from playwright.async_api import async_playwright

    monkeypatch.setattr(http_reader, '_PAGE_CLOSE_TIMEOUT', .05)
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context()
            original = await context.new_page()
            await original.set_content('<input value="keep me">')
            temporary = await context.new_page()
            closed_targets = []
            monitor = await browser.new_browser_cdp_session()
            monitor.on('Target.targetDestroyed', lambda params:
                       closed_targets.append(params['targetId']))
            await monitor.send('Target.setDiscoverTargets', {'discover': True})
            session = await context.new_cdp_session(temporary)
            target = (await session.send('Target.getTargetInfo'))['targetInfo']['targetId']
            await session.detach()

            async def close_stalls():
                await asyncio.Future()

            monkeypatch.setattr(temporary, 'close', close_stalls)

            async def page_factory():
                return temporary

            async def observe(_):
                if observer_fails:
                    raise ValueError('observation failed')
                return ReadResponse()

            reader = http_reader.ChatHTTPReader(page_factory=page_factory)
            if observer_fails:
                with pytest.raises(ValueError, match='observation failed'):
                    await reader._read(context, None, observe)
            else:
                assert await reader._read(context, None, observe) == b'{"ok":true}'
            assert context.pages == [original]
            assert original.url == 'about:blank'
            assert await original.locator('input').input_value() == 'keep me'
            assert temporary.is_closed() and not reader._pending_closes
            # Flush the independent monitor session before inspecting its events.
            await monitor.send('Target.getTargets')
            assert closed_targets == [target]
        finally:
            await browser.close()


async def test_parallel_history_bootstrap_keeps_original_tabs():
    """Bounded load uses four isolated contexts and 24 real history bootstrap tabs."""
    from playwright.async_api import async_playwright

    from anywhere_computer.subchat_browser.history import observe_history
    from anywhere_computer.subchat_state import SubchatSubmission

    submission = SubchatSubmission(operation_id='a' * 32, prompt='fixture', model='fixture',
        effort='fixture', state='submitted',
        conversation_id='00000000-0000-0000-0000-000000000001', user_message_id='user')
    path = '/backend-api/conversations/' + submission.conversation_id

    async def exercise(browser):
        context = await browser.new_context()
        try:
            original = await context.new_page()
            await original.set_content('<input value="keep me">')
            reads = []

            async def route(request):
                reads.append(request.request.method)
                if request.request.url.endswith(path):
                    await request.fulfill(content_type='application/json', body='{"ok":true}')
                else:
                    await request.fulfill(content_type='text/html', body='<script>fetch(' +
                        json.dumps(path) + ',{headers:{Authorization:"Bearer fixture"}})</script>')

            await context.route('https://chatgpt.com/**', route)

            async def observe(page):
                return await observe_history(page, submission)

            for _ in range(6):
                reader = http_reader.ChatHTTPReader()
                assert await reader._read(context, None, observe) == b'{"ok":true}'
                assert context.pages == [original]
                assert not reader._pending_closes
            assert reads == ['GET'] * 12
            assert original.url == 'about:blank'
            assert await original.locator('input').input_value() == 'keep me'
        finally:
            await context.close()

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            await asyncio.wait_for(asyncio.gather(*(exercise(browser) for _ in range(4))), 60)
        finally:
            await browser.close()
