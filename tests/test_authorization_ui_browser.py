"""Render and exercise the authentication pages in isolated Chrome contexts.

Owner credentials live in a MemoryVault, the authorization database in a temporary
directory, and passkeys come from Chromium's virtual authenticator. Cancellation and
pending states use a page-local stub of navigator.credentials, never a real passkey.
"""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit

import pytest
from playwright.async_api import async_playwright
from test_client_tokens import MemoryVault

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.browser_authorization import BrowserAuthorization
from anywhere_computer.owner_credentials import OwnerCredentials

ORIGIN = "https://localhost"
REDIRECT = "https://client.example/callback"
VIEWPORTS = {
    "desktop": {"viewport": {"width": 1280, "height": 800}},
    "phone": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True},
    "narrow": {"viewport": {"width": 320, "height": 640}, "is_mobile": True, "has_touch": True},
}
# Runs inside the page after the first submit starts: focus the label field so the test's
# Enter key is an implicit submission, then report what the page shows 900ms later.
REGISTRATION_PROBE = """
document.addEventListener('submit', () => {
  setTimeout(() => document.getElementById('passkey-label').focus(), 50);
  setTimeout(() => console.log('PROBE ' + JSON.stringify({
    status: document.getElementById('registration-status').textContent,
    headings: [...document.querySelectorAll('h1')].map(node => node.textContent),
    disabled: document.getElementById('register-passkey').disabled})), 900);
}, true);
"""
MISSING_CHROME = re.compile(r"Executable doesn't exist|distribution '\w+' is not found")
# Stand-in for a browser dialog the user has not answered yet; settled by the test.
# __skew moves Date.now() so page-side deadlines can be crossed without waiting minutes.
PENDING_STUB = """
const realNow = Date.now.bind(Date);
window.__skew = 0;
Date.now = () => realNow() + window.__skew;
window.__calls = {get: 0, create: 0};
for (const name of ['get', 'create']) {
  navigator.credentials[name] = () => new Promise((resolve, reject) => {
    window.__calls[name] += 1;
    window.__settle = {resolve, reject};
  });
}
"""


def _tools(count):
    named = ["files_read", "files_write", "terminal_start", "gui_click", "browser_navigate",
             "devices_call", "mcp_call", "codex_plugin_call", "subchat_send", "settings_update"]
    extra = [f"read_only_tool_{index:03d}" for index in range(max(0, count - len(named)))]
    tools = frozenset((named + extra)[:count])
    assert len(tools) == count
    return tools


class RequestLog(list):
    """Backend calls seen by the routed origin; `hold` delays only the browser's response."""

    hold: asyncio.Event | None = None

    def __init__(self):
        super().__init__()
        self.locations = []

    def posts(self, path):
        return [item for item in self if item[:2] == ("POST", path)]


def _query(tools):
    return urlencode({
        "response_type": "code", "client_id": "client", "redirect_uri": REDIRECT,
        "resource": ORIGIN + "/mcp", "scope": " ".join(sorted(tools)), "state": "ui-state",
        "code_challenge": pkce_s256("a" * 43), "code_challenge_method": "S256",
    })


@asynccontextmanager
async def _chrome(tmp_path, tools):
    resource = ORIGIN + "/mcp"
    store = AuthorizationStore(tmp_path, resource=resource, known_tools=tools)
    store.register_client("client", frozenset({REDIRECT}))
    store.enroll_device("owner", "device", tools)
    owner = OwnerCredentials(tmp_path, resource=resource, owner="owner", vault=MemoryVault())
    owner.initialize("owner-password")
    consent = BrowserAuthorization(store, owner, device="device")
    log = RequestLog()

    async def route_request(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.hostname == "client.example":
            await route.fulfill(status=200, body="callback")
            return
        handler = consent.routes().get(parsed.path)
        assert handler is not None
        body = request.post_data_buffer or b""
        status, content, headers = await handler(
            request.method, {key.lower(): value for key, value in request.headers.items()},
            body, parsed.query)
        # The real backend has already decided; only the response to the page is delayed.
        log.append((request.method, parsed.path, status))
        if status == 303:
            # The callback host is external: keep the real decision, stay on the origin.
            log.locations.append(headers["Location"])
            headers = {key: value for key, value in headers.items() if key != "Location"}
            status, content = 200, b"callback"
        if request.method == "POST" and log.hold is not None:
            await log.hold.wait()
        # Any Playwright error here fails the test: the old double-submit defect is
        # detected by its second backend POST, not by tolerating aborted requests.
        await route.fulfill(status=status, body=content or b"", headers=headers)

    try:
        async with async_playwright() as driver:
            try:
                browser = await driver.chromium.launch(channel="chrome", headless=True)
            except Exception as exc:
                if MISSING_CHROME.search(str(exc)) is None:
                    raise  # Only an absent Chrome skips; any other launch failure is a defect.
                pytest.skip(f"Chrome unavailable: {exc}")
            try:
                async def new_page(*, stub=False, probe="", **options):
                    context = await browser.new_context(**options)
                    await context.route("https://**/*", route_request)
                    page = await context.new_page()
                    page.violations, page.probes = [], []

                    def on_console(message):
                        if message.text.startswith("PROBE "):
                            page.probes.append(json.loads(message.text[6:]))
                        elif "Content Security Policy" in message.text:
                            page.violations.append(message.text)

                    page.on("console", on_console)
                    page.on("pageerror", lambda error: page.violations.append(str(error)))
                    if probe:
                        # Playwright calls block while a form POST response is held, but the
                        # page keeps running: in-page timers act and report through console.
                        await page.add_init_script(probe)
                    if stub:
                        await page.add_init_script(PENDING_STUB)
                    else:
                        cdp = await context.new_cdp_session(page)
                        await cdp.send("WebAuthn.enable")
                        await cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
                            "protocol": "ctap2", "transport": "internal",
                            "hasResidentKey": True, "hasUserVerification": True,
                            "isUserVerified": True, "automaticPresenceSimulation": True}})
                    return page

                yield consent, store, log, new_page
            finally:
                await browser.close()
    finally:
        store.close()


async def _enroll(consent, page):
    ticket = consent.passkeys.issue_local_ticket("owner-password")
    await page.goto(ORIGIN + "/owner-passkey?" + urlencode({"ticket": ticket}))
    await page.locator("#register-passkey").click()
    await page.get_by_text("Passkey registered.").wait_for(timeout=20000)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", list(VIEWPORTS))
async def test_consent_with_123_tools_keeps_approval_reachable_and_scope_inspectable(
    tmp_path, size,
):
    tools = _tools(123)
    async with _chrome(tmp_path, tools) as (consent, _, _, new_page):
        page = await new_page(**VIEWPORTS[size])
        await _enroll(consent, page)
        await page.goto(ORIGIN + "/authorize?" + _query(tools))
        viewport = VIEWPORTS[size]["viewport"]

        summary = page.locator("summary", has_text="Requested tools")
        assert await summary.inner_text() == "Requested tools (123)"
        assert "Device" in await page.locator(".facts").inner_text()
        # A long list starts collapsed so it cannot push authentication out of reach.
        assert not await page.locator("details", has=summary).evaluate("node => node.open")
        assert not await page.locator(".tools-scroll").is_visible()
        for warning in ("Run commands as this device user", "Call other connected services",
                        "Plugin and direct MCP tools can invoke"):
            assert await page.get_by_text(warning).first.is_visible()
        assert await page.locator("#passkey-approve").is_visible()
        assert "Bluetooth" in await page.locator("#passkey-hint").inner_text()
        assert "QR code only starts the phone prompt; nothing is approved" in await page.locator(
            "#passkey-hint").inner_text()

        # The single reading flow keeps permission inspection before owner verification
        # on every viewport. Each decision remains reachable by normal focus/scroll.
        order = await page.evaluate("""() => {
          const after = (a, b) => Boolean(document.querySelector(a).compareDocumentPosition(
            document.querySelector(b)) & Node.DOCUMENT_POSITION_FOLLOWING);
          return [after('.perm', '.tools'), after('.tools', '#approval'),
                  after('#approval', '.more')];
        }""")
        assert order == [True, True, True]
        entry = await summary.bounding_box()
        assert 44 <= entry["height"] <= 56
        approval = await page.locator("#approval").bounding_box()
        assert entry["y"] < approval["y"]
        assert await page.locator("a[href='#approval']").count() == 0
        for selector in ("#password", "button[name=approve]", "#passkey-approve",
                         "button[name=deny]"):
            await page.locator(selector).focus()
            box = await page.locator(selector).bounding_box()
            assert box["y"] >= 0 and box["y"] + box["height"] <= viewport["height"]

        # The exact list stays available, bounded, keyboard-reachable, and complete.
        await summary.click()
        assert await page.locator(".tools-scroll li").count() == 123
        region = page.locator(".tools-scroll")
        assert await region.get_attribute("tabindex") == "0"
        assert await region.get_attribute("aria-label") == "Complete list of requested tools"
        assert await region.evaluate("node => node.clientHeight <= 320")
        assert await region.evaluate("node => node.scrollHeight > node.clientHeight")

        assert await page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        for button in await page.locator("button:visible").all():
            box = await button.bounding_box()
            assert box["height"] >= 44 and box["width"] >= 44
        assert page.violations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ["desktop", "narrow"])
async def test_error_and_registration_pages_fit_viewport_without_csp_violations(tmp_path, size):
    tools = _tools(3)
    async with _chrome(tmp_path, tools) as (consent, _, _, new_page):
        page = await new_page(**VIEWPORTS[size])
        ticket = consent.passkeys.issue_local_ticket("owner-password")
        await page.goto(ORIGIN + "/owner-passkey?" + urlencode({"ticket": ticket}))
        await page.get_by_role("button", name="Register on your phone").click()
        await page.locator("#phone-registration svg").wait_for()
        pages = [await page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth")]
        await page.goto(ORIGIN + "/owner-passkey?ticket=invalid")
        heading = page.get_by_role("heading", name="Registration link is invalid or expired")
        assert await heading.is_visible()
        assert "owner-passkey-enroll" in await page.locator("main").inner_text()
        pages.append(await page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
        await page.goto(ORIGIN + "/authorize?" + _query(_tools(3)))
        pages.append(await page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
        assert pages == [True, True, True]
        assert page.violations == []


@pytest.mark.asyncio
async def test_pending_passkey_blocks_duplicate_clicks_and_restores_without_retry(tmp_path):
    tools = _tools(3)
    async with _chrome(tmp_path, tools) as (consent, store, log, new_page):
        # Enrollment uses the virtual authenticator; the consent page then shows the passkey
        # option, while a separate stubbed page stands in for an unanswered browser dialog.
        await _enroll(consent, await new_page())
        page = await new_page(stub=True)
        await page.goto(ORIGIN + "/authorize?" + _query(tools))
        passkey, approve = page.locator("#passkey-approve"), page.locator("button[name=approve]")
        status = page.locator("#submit-status")
        decisions = lambda: [item for item in log if item[:2] == ("POST", "/authorize")]  # noqa: E731

        await passkey.click()
        assert await passkey.get_attribute("aria-disabled") == "true"
        assert await passkey.inner_text() == "Waiting for passkey…"
        assert await approve.is_disabled()
        assert await status.get_attribute("data-state") == "pending"
        assert "Nothing is approved until it finishes" in await status.inner_text()
        await passkey.click(force=True)
        await passkey.press("Enter")
        assert await page.evaluate("window.__calls.get") == 1

        await page.evaluate(
            "window.__settle.reject(new DOMException('dismissed', 'NotAllowedError'))")
        await status.get_by_text("cancelled or timed out").wait_for()
        assert await status.get_attribute("data-state") == "error"
        assert await passkey.get_attribute("aria-disabled") is None
        assert await passkey.inner_text() == "Allow with passkey"
        assert await approve.is_enabled()
        await page.wait_for_timeout(500)
        # Cancellation neither retries the ceremony nor posts a decision.
        assert await page.evaluate("window.__calls.get") == 1
        assert decisions() == []
        assert store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0

        await passkey.click()
        assert await page.evaluate("window.__calls.get") == 2
        await page.evaluate("window.__settle.reject(new TypeError('unsupported'))")
        await status.get_by_text("cancelled or unavailable").wait_for()
        assert "Nothing was approved" in await status.inner_text()
        assert decisions() == []
        assert len(consent.pending) == 1
        assert page.violations == []


@pytest.mark.asyncio
async def test_pending_registration_blocks_duplicates_and_cancel_saves_nothing(tmp_path):
    tools = _tools(3)
    async with _chrome(tmp_path, tools) as (consent, _, log, new_page):
        page = await new_page(stub=True)
        ticket = consent.passkeys.issue_local_ticket("owner-password")
        await page.goto(ORIGIN + "/owner-passkey?" + urlencode({"ticket": ticket}))
        button, phone = page.locator("#register-passkey"), page.locator("#register-on-phone")
        status = page.locator("#registration-status")

        # An invalid label never reaches the browser's passkey dialog.
        await page.locator("#passkey-label").fill("")
        await button.click()
        assert await page.evaluate("window.__calls.create") == 0
        await page.locator("#passkey-label").fill("Laptop")

        await button.click()
        assert await button.get_attribute("aria-disabled") == "true"
        assert await button.inner_text() == "Waiting for passkey…"
        assert await phone.is_disabled()
        assert await status.get_attribute("data-state") == "pending"
        await button.click(force=True)
        assert await page.evaluate("window.__calls.create") == 1

        await page.evaluate(
            "window.__settle.reject(new DOMException('dismissed', 'NotAllowedError'))")
        await status.get_by_text("Nothing was saved").wait_for()
        assert await button.inner_text() == "Register passkey"
        assert await button.get_attribute("aria-disabled") is None
        assert await phone.is_enabled()
        await page.wait_for_timeout(500)
        assert await page.evaluate("window.__calls.create") == 1
        assert [item for item in log if item[:2] == ("POST", "/owner-passkey")] == []
        assert consent.passkeys.list() == []
        assert consent.passkeys.ticket_valid(ticket)
        assert not await page.locator("#registration-response").evaluate("node => node.value")
        assert page.violations == []


@pytest.mark.asyncio
async def test_registration_posts_once_while_server_verifies_then_shows_verified_success(
    tmp_path,
):
    async with _chrome(tmp_path, _tools(3)) as (consent, _, log, new_page):
        page = await new_page(probe=REGISTRATION_PROBE)
        ticket = consent.passkeys.issue_local_ticket("owner-password")
        await page.goto(ORIGIN + "/owner-passkey?" + urlencode({"ticket": ticket}))
        box = await page.locator("#register-passkey").bounding_box()
        log.hold = asyncio.Event()
        try:
            await page.locator("#register-passkey").click(no_wait_after=True)
            for _ in range(100):
                if log.posts("/owner-passkey"):
                    break
                await asyncio.sleep(0.1)
            # The real backend has verified and saved; the browser has no answer yet.
            assert [item[2] for item in log.posts("/owner-passkey")] == [200]
            assert len(consent.passkeys.list()) == 1
            assert not consent.passkeys.ticket_valid(ticket)
            # Duplicate user input while the response is pending: Enter in the (now
            # focused) label field and a second real click on the same button.
            await asyncio.sleep(0.2)
            await page.keyboard.press("Enter")
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await asyncio.sleep(1.3)
            assert len(log.posts("/owner-passkey")) == 1
            # No success is claimed before the existing verified-save response arrives.
            assert len(page.probes) == 1
            assert "Verifying and saving" in page.probes[0]["status"]
            assert page.probes[0]["headings"] == ["Register an owner passkey"]
            assert page.probes[0]["disabled"] is True
        finally:
            log.hold.set()
        await page.get_by_role("heading", name="Passkey registered.").wait_for(timeout=10000)
        content = await page.locator("main").inner_text()
        assert "updates after it confirms the result" in content
        assert "within a few seconds" not in content
        assert [item[2] for item in log.posts("/owner-passkey")] == [200]
        assert len(consent.passkeys.list()) == 1
        assert not consent.passkeys.ticket_valid(ticket)
        assert page.violations == []


CREDENTIAL = (
    "{id: 'AAAA', rawId: new ArrayBuffer(8), type: 'public-key', response: {"
    "clientDataJSON: new ArrayBuffer(4), authenticatorData: new ArrayBuffer(4), "
    "signature: new ArrayBuffer(4), userHandle: null}}"
)
LATE_ANSWER = {
    "resolve": f"window.__settle.resolve({CREDENTIAL})",
    "reject": "window.__settle.reject(new DOMException('late', 'NotAllowedError'))",
}


async def _pending_passkey_page(consent, new_page, tools, probe=""):
    await _enroll(consent, await new_page())
    page = await new_page(stub=True, probe=probe)
    await page.goto(ORIGIN + "/authorize?" + _query(tools))
    await page.locator("#passkey-approve").click()
    assert await page.locator("#passkey-approve").get_attribute("aria-disabled") == "true"
    return page


def _deny_probe(late):
    """After Deny submits: answer the open prompt late, then report the page's state."""
    return f"""
document.addEventListener('submit', () => {{
  setTimeout(() => {{ {LATE_ANSWER[late]}; }}, 300);
  setTimeout(() => {{
    const passkey = document.getElementById('passkey-approve');
    console.log('PROBE ' + JSON.stringify({{label: passkey.textContent,
      aria: passkey.getAttribute('aria-disabled'),
      status: document.getElementById('submit-status').textContent,
      assertion: document.getElementById('passkey-assertion').value,
      calls: window.__calls.get}}));
  }}, 900);
}}, true);
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("late", ["resolve", "reject"])
async def test_expiry_while_passkey_pending_restores_button_and_ignores_late_answer(
    tmp_path, late,
):
    tools = _tools(3)
    async with _chrome(tmp_path, tools) as (consent, store, log, new_page):
        page = await _pending_passkey_page(consent, new_page, tools)
        passkey, status = page.locator("#passkey-approve"), page.locator("#submit-status")
        await page.evaluate("window.__skew = 301000")
        await status.get_by_text("This request expired").wait_for(timeout=5000)
        assert await status.get_attribute("data-state") == "error"
        assert await passkey.inner_text() == "Allow with passkey"
        assert await passkey.get_attribute("aria-disabled") is None
        for selector in ("#passkey-approve", "button[name=approve]", "button[name=deny]"):
            assert await page.locator(selector).is_disabled()

        await page.evaluate(LATE_ANSWER[late])
        await page.wait_for_timeout(500)
        assert "This request expired" in await status.inner_text()
        assert await page.evaluate("window.__calls.get") == 1
        assert not await page.locator("#passkey-assertion").evaluate("node => node.value")
        assert log.posts("/authorize") == []
        assert store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
        # Server-side expiry is untouched: the client only stops offering dead controls.
        assert len(consent.pending) == 1
        assert page.violations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("late", ["resolve", "reject"])
async def test_deny_while_passkey_pending_ignores_late_answer_without_extra_post(
    tmp_path, late,
):
    tools = _tools(3)
    async with _chrome(tmp_path, tools) as (consent, store, log, new_page):
        page = await _pending_passkey_page(consent, new_page, tools, probe=_deny_probe(late))
        log.hold = asyncio.Event()
        try:
            # The held response keeps the navigation open; the page itself answers the
            # open prompt late (in-page timer) and reports what it then shows.
            await page.locator("button[name=deny]").click(no_wait_after=True)
            for _ in range(100):
                if page.probes:
                    break
                await asyncio.sleep(0.1)
            assert [item[2] for item in log.posts("/authorize")] == [303]
            assert len(page.probes) == 1
            state = page.probes[0]
            assert state["label"] == "Allow with passkey" and state["aria"] is None
            assert "Processing your decision" in state["status"]
            assert state["assertion"] == "" and state["calls"] == 1
            # The late answer neither posted again nor created a grant.
            assert len(log.posts("/authorize")) == 1
            assert store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
        finally:
            log.hold.set()
        await page.get_by_text("callback", exact=True).wait_for()
        assert len(log.locations) == 1 and "error=access_denied" in log.locations[0]
        assert len(log.posts("/authorize")) == 1
        assert store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
        assert consent.pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("width, height", [(1280, 700), (1280, 900), (390, 844), (320, 640)])
async def test_error_state_keeps_every_decision_and_status_reachable_by_keyboard(
    tmp_path, width, height,
):
    tools = _tools(123)
    mobile = width < 900
    options = {"viewport": {"width": width, "height": height}, "is_mobile": mobile,
               "has_touch": mobile}
    async with _chrome(tmp_path, tools) as (consent, _, _, new_page):
        await _enroll(consent, await new_page())
        page = await new_page(stub=True, **options)
        await page.goto(ORIGIN + "/authorize?" + _query(tools))
        await page.locator("#password").fill("wrong-password")
        await page.locator("button[name=approve]").click()
        alert = page.locator("#auth-error")
        await alert.wait_for()
        assert await page.evaluate("document.activeElement.id") == "password"

        async def inside_viewport(selector):
            box = await page.locator(selector).bounding_box()
            visible = await page.evaluate("[window.innerHeight, scrollY]")
            assert box["y"] >= -1 and box["y"] + box["height"] <= height + 1, (box, visible)
            return True

        # Tab order follows the DOM: show toggle, password submit, passkey, then Deny.
        reached = []
        for _ in range(12):
            await page.keyboard.press("Tab")
            focused = await page.evaluate(
                "document.activeElement.id || document.activeElement.name")
            reached.append(focused)
            assert await inside_viewport(":focus"), reached
            if focused == "deny":
                break
        assert reached[-4:] == ["password-visibility", "approve", "passkey-approve", "deny"]
        # A pending prompt's status is brought into view after normal page scrolling.
        await page.locator("#passkey-approve").focus()
        await page.keyboard.press("Enter")
        await page.locator("#submit-status").get_by_text("Waiting for your passkey").wait_for()
        assert await inside_viewport("#submit-status")
        assert await page.locator("#passkey-approve").get_attribute("aria-disabled") == "true"
        assert page.violations == []
