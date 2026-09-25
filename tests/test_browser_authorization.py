import asyncio
import re
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from test_authorization import REDIRECT, RESOURCE, VERIFIER, pkce_s256, redeem
from test_authorization import authority as authority
from test_client_tokens import MemoryVault

from anywhere_computer.browser_authorization import BrowserAuthorization
from anywhere_computer.owner_credentials import OwnerCredentials


@pytest.fixture
def browser(authority, tmp_path, monkeypatch):
    credentials = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    # Authentication binding tests use a deterministic verifier; the real scrypt
    # implementation and persistence are tested in test_owner_credentials.
    monkeypatch.setattr(credentials, "verify", lambda password: password == "synthetic-password")
    return BrowserAuthorization(authority, credentials, device="device")


async def begin(browser, **overrides):
    query = urlencode(
        dict(
            response_type="code",
            client_id="client",
            redirect_uri=REDIRECT,
            resource=RESOURCE,
            scope="files_read",
            state="client-state",
            code_challenge=pkce_s256(VERIFIER),
            code_challenge_method="S256",
            **overrides,
        )
    )
    status, body, headers = await browser.authorize("GET", {}, b"", query)
    assert status == 200
    assert headers["Referrer-Policy"] == "same-origin"
    fields = dict(re.findall(r"name=(request_id|csrf) value='([^']+)'", body.decode()))
    request_headers = {
        "origin": "https://computer.example",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": headers["Set-Cookie"].split(";", 1)[0],
    }
    return fields, request_headers


async def decide(browser, fields, headers, **overrides):
    return await browser.authorize(
        "POST",
        headers,
        urlencode(
            {**fields, "approve": "yes", "password": "synthetic-password", **overrides}
        ).encode(),
    )


async def test_consent_issues_one_bound_code(browser, authority):
    fields, headers = await begin(browser)
    results = await asyncio.gather(
        decide(browser, fields, headers), decide(browser, fields, headers)
    )
    assert sorted(item[0] for item in results) == [303, 403]
    success = next(item for item in results if item[0] == 303)
    callback = parse_qs(urlsplit(success[2]["Location"]).query)
    assert callback["state"] == ["client-state"]
    assert callback["iss"] == ["https://computer.example"]
    token = redeem(authority, callback["code"][0])
    assert authority.verify(token.value, resource=RESOURCE).tools == frozenset({"files_read"})


async def test_consent_explains_owner_password_and_keeps_tools_reviewable(browser):
    fields, _ = await begin(browser)
    record = browser.pending[fields["request_id"]]
    page = browser._page(fields["request_id"], record, fields["csrf"]).decode()
    assert "Anywhere Computer owner password" in page
    assert "not your computer or ChatGPT login password" in page
    assert "at least 8 characters" in page
    assert "<details open><summary>Requested tools (1)</summary>" in page
    assert "<li><code>files_read</code></li>" in page
    assert page.index("This connection can read data") < page.index(
        "Requested tools (1)"
    )
    assert "anywhere http-revoke" in page
    assert "Access will be sent to <strong>client.example</strong>" in page
    assert "<details><summary>Technical details</summary>" in page
    assert "required>" in page and "formnovalidate>Deny" in page
    assert "button.disabled = true" in page


async def test_consent_warns_when_client_omits_available_subchat_tools(tmp_path, monkeypatch):
    from anywhere_computer.authorization import AuthorizationStore

    tools = frozenset({"files_read", "subchat_send", "subchat_recover"})
    store = AuthorizationStore(tmp_path / "auth", resource=RESOURCE, known_tools=tools)
    store.register_client("client", frozenset({REDIRECT}))
    store.enroll_device("owner", "device", tools)
    credentials = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    monkeypatch.setattr(credentials, "verify", lambda password: password == "synthetic-password")
    consent = BrowserAuthorization(store, credentials, device="device")
    try:
        query = urlencode({
            "response_type": "code", "client_id": "client", "redirect_uri": REDIRECT,
            "resource": RESOURCE, "scope": "files_read", "state": "client-state",
            "code_challenge": pkce_s256(VERIFIER), "code_challenge_method": "S256",
        })
        status, page, response_headers = await consent.authorize("GET", {}, b"", query)
        assert status == 200
        assert b"Some Subchat tools available on this device were not requested" in page
        assert b"Approving this page will not grant them" in page
        assert b"<li><code>subchat_send</code></li>" not in page
        fields = dict(re.findall(r"name=(request_id|csrf) value='([^']+)'", page.decode()))
        status, _, approved = await decide(consent, fields, {
            "origin": "https://computer.example",
            "content-type": "application/x-www-form-urlencoded",
            "cookie": response_headers["Set-Cookie"].split(";", 1)[0],
        })
        assert status == 303
        code = parse_qs(urlsplit(approved["Location"]).query)["code"][0]
        token = redeem(store, code)
        assert store.verify(token.value, resource=RESOURCE).tools == frozenset({"files_read"})

        complete_query = urlencode({
            "response_type": "code", "client_id": "client", "redirect_uri": REDIRECT,
            "resource": RESOURCE, "scope": "files_read subchat_send subchat_recover",
            "state": "client-state", "code_challenge": pkce_s256(VERIFIER),
            "code_challenge_method": "S256",
        })
        status, complete_page, _ = await consent.authorize("GET", {}, b"", complete_query)
        assert status == 200
        assert b"Some Subchat tools available on this device" not in complete_page
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["origin", "null_origin", "cookie", "csrf", "password"])
async def test_invalid_consent_does_not_consume_request(browser, failure):
    fields, headers = await begin(browser)
    changed_fields, changed_headers = dict(fields), dict(headers)
    overrides = {}
    if failure == "null_origin":
        changed_headers["origin"] = "null"
    elif failure in {"origin", "cookie"}:
        changed_headers[failure] = "invalid"
    elif failure == "csrf":
        changed_fields["csrf"] = "invalid"
    else:
        overrides["password"] = "wrong-secret-never-echo"
    status, body, response_headers = await decide(
        browser, changed_fields, changed_headers, **overrides
    )
    assert status == 403
    assert b"wrong-secret-never-echo" not in body
    if failure == "password":
        assert b"Check the Anywhere Computer owner password" in body
        assert b"anywhere owner-reset" in body
    assert "Location" not in response_headers
    assert (await decide(browser, fields, headers))[0] == 303


async def test_revocation_between_form_and_decision_prevents_code(browser, authority):
    fields, headers = await begin(browser)
    authority.revoke_device(owner="owner", device="device")
    status, _, response_headers = await decide(browser, fields, headers)
    assert status == 400
    assert "Location" not in response_headers


@pytest.mark.parametrize("during_password", [False, True])
async def test_reenable_rejects_old_form_even_during_password_verification(
    browser, authority, monkeypatch, during_password
):
    fields, headers = await begin(browser)
    entered, release = threading.Event(), threading.Event()

    def paused_verify(password):
        entered.set()
        release.wait()
        return True

    if during_password:
        monkeypatch.setattr(browser.credentials, "verify", paused_verify)
        pending = asyncio.create_task(decide(browser, fields, headers))
    try:
        if during_password:
            assert await asyncio.to_thread(entered.wait, 30)
            assert not pending.done()
        authority.revoke_device(owner="owner", device="device")
        assert authority.enable_device(owner="owner", device="device")
    finally:
        release.set()
    result = await pending if during_password else await decide(browser, fields, headers)
    assert result[0] == 400 and "Location" not in result[2]
    assert authority.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
    new_fields, new_headers = await begin(browser)
    assert (await decide(browser, new_fields, new_headers))[0] == 303


async def test_deny_requires_browser_binding_but_no_password(browser):
    fields, headers = await begin(browser)
    status, _, response_headers = await browser.authorize(
        "POST", headers, urlencode({**fields, "deny": "yes"}).encode()
    )
    assert status == 303
    callback = parse_qs(urlsplit(response_headers["Location"]).query)
    assert callback == {"state": ["client-state"], "error": ["access_denied"],
                        "iss": ["https://computer.example"]}
    assert (await decide(browser, fields, headers))[0] == 403


async def test_registered_redirect_cannot_inject_duplicate_issuer(browser, authority):
    redirect = REDIRECT + "?iss=https%3A%2F%2Fevil.example"
    authority.register_client("injected", frozenset({redirect}))
    status, _, headers = await browser.authorize("GET", {}, b"", urlencode({
        "response_type": "code", "client_id": "injected", "redirect_uri": redirect,
        "resource": RESOURCE, "scope": "files_read", "state": "selected",
        "code_challenge": pkce_s256(VERIFIER), "code_challenge_method": "S256",
    }))
    assert status == 400 and "Location" not in headers


def test_issuer_preserves_discovery_authority_exactly(tmp_path):
    from anywhere_computer.authorization import AuthorizationStore
    from anywhere_computer.oauth_endpoints import OAuthEndpoints

    resource = "https://Computer.Example:443/mcp"
    store = AuthorizationStore(tmp_path / "auth", resource=resource, known_tools=frozenset())
    try:
        owner = OwnerCredentials(tmp_path, resource=resource, owner="owner", vault=MemoryVault())
        consent = BrowserAuthorization(store, owner, device="device")
        metadata = OAuthEndpoints(store, authorization_endpoint=consent.authorization_endpoint)
        assert consent.origin == "https://computer.example"
        assert consent.issuer == metadata.issuer == "https://Computer.Example:443"
    finally:
        store.close()


async def test_failed_attempts_do_not_lock_another_browser(browser):
    first_fields, first_headers = await begin(browser)
    other_fields, other_headers = await begin(browser)
    for _ in range(10):
        assert (await decide(browser, first_fields, first_headers, password="wrong"))[0] == 403
    assert (await decide(browser, first_fields, first_headers))[0] == 429
    assert (await decide(browser, other_fields, other_headers))[0] == 303


async def test_busy_password_workers_do_not_queue_verifications(browser):
    fields, headers = await begin(browser)
    assert browser.password_slots.acquire(blocking=False)
    assert browser.password_slots.acquire(blocking=False)
    try:
        assert (await decide(browser, fields, headers))[0] == 503
    finally:
        browser.password_slots.release()
        browser.password_slots.release()
    assert (await decide(browser, fields, headers))[0] == 303
    assert browser.authorization_endpoint == "https://computer.example/authorize"


def test_consent_csp_allows_only_callback_path_and_pinned_script():
    import base64
    import hashlib

    from anywhere_computer.browser_authorization import _PASSWORD_VISIBILITY_SCRIPT

    headers = BrowserAuthorization._headers('https://chatgpt.com/connector_platform_oauth_redirect')
    policy = headers['Content-Security-Policy']
    assert "form-action 'self' https://chatgpt.com/connector_platform_oauth_redirect;" in policy
    digest = base64.b64encode(
        hashlib.sha256(_PASSWORD_VISIBILITY_SCRIPT.encode()).digest()
    ).decode()
    assert f"script-src 'sha256-{digest}';" in policy
    assert "script-src 'unsafe-inline'" not in policy
    assert "form-action 'self';" in BrowserAuthorization._headers()['Content-Security-Policy']
    with pytest.raises(ValueError):
        BrowserAuthorization._headers('https://host;evil.example/callback')
