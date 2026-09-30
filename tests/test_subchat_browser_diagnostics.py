"""Keep the actual blocked owned tab inspectable without replaying its request."""

import asyncio

import pytest
from playwright.async_api import async_playwright

from anywhere_computer.subchat import SubchatPreparationFailed
from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend


@pytest.mark.parametrize('source', ['http', 'ui'])
async def test_challenge_inspection_preserves_tab_and_rejection(tmp_path, source):
    requests = []

    async def challenge(route):
        requests.append(route.request.url)
        await route.fulfill(status=403, headers={'cf-mitigated': 'challenge'},
                            content_type='text/html', body='<title>Blocked fixture</title>')

    async with async_playwright() as driver:
        context = await driver.chromium.launch_persistent_context(
            str(tmp_path / 'chrome'), channel='chrome', headless=True)
        original = context.pages[0]
        await context.route('https://chatgpt.com/**', challenge)
        backend = BrowserSubchatBackend(context, http_read=True,
                                       httpx_generation=True, background_pages=True)
        try:
            # Inspection must not open a browser/tab before an actual failure.
            assert (await backend.browser_diagnostics('show'))['state'] == 'unavailable'
            if source == 'ui':
                assert (await backend.catalog())['reason'] == 'browser_challenge'
            else:
                with pytest.raises(SubchatPreparationFailed):
                    await backend.http_catalog()
            assert len(context.pages) == 2
            blocked = next(page for page in context.pages if page is not original)
            assert await blocked.title() == 'Blocked fixture'
            status = await backend.browser_diagnostics('status')
            assert status['state'] == 'retained'
            assert backend.has_live_diagnostics() and backend.has_live_transport()
            assert not backend.has_live_generation()
            assert status['automatic_retry'] is False
            assert 0 < status['remaining_seconds'] <= 300
            shown = await backend.browser_diagnostics('show')
            assert shown['state'] == 'display_requested'
            assert shown['challenge_cleared'] is False
            assert backend._http_reader.blocked_navigation_reason == 'browser_challenge'
            with pytest.raises(SubchatPreparationFailed):
                await backend.http_catalog()
            assert requests == ['https://chatgpt.com/']
            await backend.browser_diagnostics('close')
            assert context.pages == [original]
            assert not backend.has_live_transport()
            assert backend._http_reader.blocked_navigation_reason == 'browser_challenge'
            assert (await backend.browser_diagnostics('status'))['state'] == 'unavailable'
        finally:
            await backend.close_generations()
            await context.close()


@pytest.mark.parametrize('read_only', [False, True])
async def test_local_mcp_diagnostics_obey_catalog_and_never_send(tmp_path, read_only):
    from test_subchat_lifecycle import BrowserFixture

    from anywhere_computer.models import Request
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import Subchats
    from anywhere_computer.subchat_mcp import direct_gateway_catalog, session
    from anywhere_computer.subchat_state import SubchatSubmissions

    class DiagnosticFixture(BrowserFixture):
        browser_diagnostics_available = True

        async def browser_diagnostics(self, action):
            return {'state': 'unavailable', 'automatic_retry': False}

    ledger = Ledger(tmp_path)
    backend = DiagnosticFixture()
    server = session(Subchats(SubchatSubmissions(ledger.connection), backend),
                     owner='owner', read_only=read_only)
    try:
        tools = await server.catalog()
        names = [tool['name'] for tool in tools]
        assert ('subchat_browser_diagnostics' in names) is not read_only
        assert 'subchat_browser_diagnostics' not in [
            tool['name'] for tool in direct_gateway_catalog()]
        reply = await server.execute(Request(operation_id='a' * 32,
            tool='subchat_browser_diagnostics', arguments={'action': 'show'}))
        assert reply.state == ('failed' if read_only else 'completed')
        assert backend.sends == 0
        if not read_only:
            invalid = await server.execute(Request(operation_id='b' * 32,
                tool='subchat_browser_diagnostics', arguments={'action': 'show',
                                                              'url': 'https://other.invalid'}))
            assert invalid.state == 'failed'
    finally:
        await server.close()
        ledger.close()


async def test_diagnostic_retention_expires_and_shutdown_closes_it(tmp_path, monkeypatch):
    from anywhere_computer.subchat_browser import diagnostics

    monkeypatch.setattr(diagnostics, 'RETENTION_SECONDS', .05)
    async with async_playwright() as driver:
        context = await driver.chromium.launch_persistent_context(
            str(tmp_path / 'chrome'), channel='chrome', headless=True)
        backend = BrowserSubchatBackend(context, http_read=True,
                                       httpx_generation=True, background_pages=True)
        try:
            page = await backend._new_page()
            await backend._diagnostics.retain(page)
            await asyncio.sleep(.1)
            assert page.is_closed()
            another = await backend._new_page()
            await backend._diagnostics.retain(another)
            await backend.close_generations()
            assert another.is_closed()
        finally:
            await backend.close_generations()
            await context.close()
