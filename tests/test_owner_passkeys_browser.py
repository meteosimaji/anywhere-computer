"""Exercise the real consent scripts with Chromium's isolated WebAuthn device."""

from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.async_api import async_playwright
from test_client_tokens import MemoryVault

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.browser_authorization import BrowserAuthorization
from anywhere_computer.owner_credentials import OwnerCredentials
from anywhere_computer.owner_passkeys import OwnerPasskeys


@pytest.mark.asyncio
@pytest.mark.parametrize("passkey_only", [False, True])
async def test_virtual_authenticator_registers_and_approves_consent(tmp_path, passkey_only):
    # A routed HTTPS origin gives Chromium a secure context without touching the
    # user's keychain, passkeys, network service, or persisted HTTP configuration.
    origin = "https://localhost"
    resource = origin + "/mcp"
    redirect = "https://client.example/callback"
    verifier = "a" * 43
    store = AuthorizationStore(tmp_path, resource=resource, known_tools=frozenset({"files_read"}))
    store.register_client("client", frozenset({redirect}))
    store.enroll_device("owner", "device", frozenset({"files_read"}))
    owner = OwnerCredentials(tmp_path, resource=resource, owner="owner", vault=MemoryVault())
    if passkey_only:
        owner.initialize_passkey_only()
    else:
        owner.initialize("owner-password")
    consent = BrowserAuthorization(store, owner, device="device")
    passkeys = OwnerPasskeys(owner, device="device")
    ticket = (passkeys.issue_initial_local_ticket() if passkey_only else
              passkeys.issue_local_ticket("owner-password"))
    try:
        async with async_playwright() as driver:
            try:
                browser = await driver.chromium.launch(channel="chrome", headless=True)
            except Exception as exc:
                pytest.skip(f"Chromium unavailable: {exc}")
            try:
                context = await browser.new_context()
                page = await context.new_page()
                cdp = await context.new_cdp_session(page)
                await cdp.send("WebAuthn.enable")
                await cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
                    "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
                    "hasUserVerification": True, "isUserVerified": True,
                    "automaticPresenceSimulation": True,
                }})
                approvals = []

                async def route_request(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.hostname == "client.example":
                        await route.fulfill(status=200, body="callback")
                        return
                    handler = consent.routes().get(parsed.path)
                    assert handler is not None
                    status, body, headers = await handler(
                        request.method,
                        {key.lower(): value for key, value in request.headers.items()},
                        request.post_data_buffer or b"",
                        parsed.query,
                    )
                    if parsed.path == "/authorize" and request.method == "POST":
                        approvals.append((status, headers, request.post_data_buffer or b""))
                        # The registered callback is external to this test. Keep
                        # the browser on the consent origin while retaining the
                        # actual approval result and cookie expiry header.
                        if status == 303:
                            headers = {key: value for key, value in headers.items()
                                       if key != "Location"}
                            status, body = 200, b"approved"
                    await route.fulfill(status=status, body=body or b"", headers=headers)

                await context.route("https://**/*", route_request)
                await page.goto(origin + "/owner-passkey?" + urlencode({"ticket": ticket}))
                assert await page.evaluate("window.isSecureContext")
                await page.locator("#register-passkey").click()
                await page.get_by_text("Passkey registered.").wait_for(timeout=20000)
                assert len(consent.passkeys.list()) == 1
                assert not consent.passkeys.ticket_valid(ticket)

                query = urlencode({
                    "response_type": "code", "client_id": "client", "redirect_uri": redirect,
                    "resource": resource, "scope": "files_read", "state": "browser-state",
                    "code_challenge": pkce_s256(verifier), "code_challenge_method": "S256",
                })
                await page.goto(origin + "/authorize?" + query)
                if passkey_only:
                    assert await page.locator("#password").count() == 0
                identity = await page.locator("input[name=request_id]").input_value()
                assert identity in consent.pending
                await page.evaluate("""() => {
                  document.querySelector('input[name=csrf]').value = 'invalid';
                  const decision = document.createElement('input');
                  decision.type = 'hidden'; decision.name = 'passkey'; decision.value = 'yes';
                  document.querySelector('form').append(decision);
                  document.querySelector('form').submit();
                }""")
                await page.get_by_text("invalid or expired").wait_for()
                assert identity in consent.pending
                assert store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
                await page.goto(origin + "/authorize?" + query)
                identity = await page.locator("input[name=request_id]").input_value()
                await page.locator("#passkey-approve").click()
                # The pending UI also says "nothing is approved". Only the
                # completed callback document is the approval receipt.
                await page.get_by_text("approved", exact=True).wait_for()
                assert [item[0] for item in approvals] == [403, 303]
                result = parse_qs(urlsplit(approvals[1][1]["Location"]).query)
                assert result["state"] == ["browser-state"]
                assert "code" in result
                assert len(store.db.execute("SELECT id FROM grants").fetchall()) == 1
                assert identity not in consent.pending
                assert not any(cookie["name"].endswith(identity)
                               for cookie in await context.cookies(origin))
                await page.evaluate("""body => {
                  const form = document.createElement('form');
                  form.method = 'POST'; form.action = '/authorize';
                  for (const [name, value] of new URLSearchParams(body)) {
                    const field = document.createElement('input');
                    field.name = name; field.value = value; form.append(field);
                  }
                  document.body.append(form); form.submit();
                }""", approvals[1][2].decode())
                await page.get_by_text("Start again from your client").wait_for(timeout=20000)
                assert approvals[-1][0] == 403
                assert store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 1
            finally:
                await browser.close()
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("history_resume", ["none", "before_qr", "while_waiting"])
async def test_phone_registration_replaces_desktop_qr_after_verified_save(tmp_path, history_resume):
    origin = "https://localhost"
    store = AuthorizationStore(tmp_path, resource=origin + "/mcp",
                               known_tools=frozenset({"files_read"}))
    store.enroll_device("owner", "device", frozenset({"files_read"}))
    owner = OwnerCredentials(tmp_path, resource=origin + "/mcp", owner="owner",
                             vault=MemoryVault())
    owner.initialize("owner-password")
    consent = BrowserAuthorization(store, owner, device="device")
    ticket = consent.passkeys.issue_local_ticket("owner-password")
    try:
        async with async_playwright() as driver:
            try:
                browser = await driver.chromium.launch(channel="chrome", headless=True)
            except Exception as exc:
                pytest.skip(f"Chromium unavailable: {exc}")
            try:
                desktop_context = await browser.new_context()
                phone_context = await browser.new_context()
                status_requests = 0

                async def route_request(route):
                    nonlocal status_requests
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.path == "/owner-passkey-status":
                        status_requests += 1
                        if status_requests == 1:
                            # A failed observation, even with success-shaped
                            # JSON, cannot remove the QR or claim enrollment.
                            await route.fulfill(status=503, content_type="application/json",
                                                body='{"status":"registered"}')
                            return
                    handler = consent.routes().get(parsed.path)
                    assert handler is not None
                    status, body, headers = await handler(
                        request.method,
                        {key.lower(): value for key, value in request.headers.items()},
                        request.post_data_buffer or b"", parsed.query,
                    )
                    await route.fulfill(status=status, body=body or b"", headers=headers)

                await desktop_context.route("https://**/*", route_request)
                await phone_context.route("https://**/*", route_request)
                desktop = await desktop_context.new_page()
                await desktop.goto(origin + "/owner-passkey?" + urlencode({"ticket": ticket}))
                async def restore_cached_page():
                    # Exercise the browser lifecycle contract while preserving
                    # the same script/DOM, as a persisted history entry does.
                    await desktop.evaluate("""() => {
                      window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
                      window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
                    }""")
                if history_resume == "before_qr":
                    await restore_cached_page()
                await desktop.get_by_role("button", name="Register on your phone").click()
                assert await desktop.locator("#phone-registration svg").is_visible()
                await desktop.get_by_text(
                    "Cannot confirm registration yet. Retrying; do not register again."
                ).wait_for(timeout=5000)
                assert await desktop.locator("#phone-registration svg").is_visible()
                assert len(consent.passkeys.list()) == 0
                await desktop.get_by_text("Waiting for registration on your phone…").wait_for()
                assert len(consent.passkeys.list()) == 0
                if history_resume == "while_waiting":
                    await restore_cached_page()

                phone_url = await desktop.locator("#phone-registration-link").get_attribute("href")
                assert phone_url == origin + "/owner-passkey?" + urlencode({"ticket": ticket})
                phone = await phone_context.new_page()
                cdp = await phone_context.new_cdp_session(phone)
                await cdp.send("WebAuthn.enable")
                await cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
                    "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
                    "hasUserVerification": True, "isUserVerified": True,
                    "automaticPresenceSimulation": True,
                }})
                await phone.goto(phone_url)
                await phone.get_by_role("button", name="Register passkey", exact=True).click()
                await phone.get_by_text("Passkey registered.", exact=True).wait_for(timeout=20000)
                await desktop.get_by_role("heading", name="Passkey registered.").wait_for(
                    timeout=10000)
                await desktop.get_by_text(
                    "This passkey can now approve connection requests for device. "
                    "You can close this page.",
                    exact=True).wait_for()
                assert await desktop.title() == "Passkey registered. — Anywhere Computer"
                assert len(consent.passkeys.list()) == 1
                assert not await desktop.locator("#phone-registration").is_visible()
                assert await desktop.locator("#phone-registration svg").count() == 0
                assert await desktop.locator("input[name=ticket]").input_value() == ""
                assert desktop.url == origin + "/owner-passkey"
                assert await desktop.locator("#register-on-phone").is_disabled()
                # The success view is the verified-save state, not an optimistic one.
                assert await desktop.locator("#registration-status").get_attribute(
                    "data-state") == "success"
                assert not await desktop.locator("#registration-options").is_visible()
                assert await desktop.get_by_text("Waiting for registration").count() == 0
                assert await desktop.locator("h1").count() == 1
                assert not await desktop.locator("#registration-intro").is_visible()
                assert await phone.get_by_role(
                    "heading", name="Passkey registered.").is_visible()
            finally:
                await browser.close()
    finally:
        store.close()
