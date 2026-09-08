import asyncio
import re
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
    token = redeem(authority, callback["code"][0])
    assert authority.verify(token.value, resource=RESOURCE).tools == frozenset({"files_read"})


@pytest.mark.parametrize("failure", ["origin", "cookie", "csrf", "password"])
async def test_invalid_consent_does_not_consume_request(browser, failure):
    fields, headers = await begin(browser)
    changed_fields, changed_headers = dict(fields), dict(headers)
    overrides = {}
    if failure in {"origin", "cookie"}:
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
    assert "Location" not in response_headers
    assert (await decide(browser, fields, headers))[0] == 303


async def test_revocation_between_form_and_decision_prevents_code(browser, authority):
    fields, headers = await begin(browser)
    authority.revoke_device(owner="owner", device="device")
    status, _, response_headers = await decide(browser, fields, headers)
    assert status == 400
    assert "Location" not in response_headers


async def test_deny_requires_browser_binding_but_no_password(browser):
    fields, headers = await begin(browser)
    status, _, response_headers = await browser.authorize(
        "POST", headers, urlencode({**fields, "deny": "yes"}).encode()
    )
    assert status == 303
    callback = parse_qs(urlsplit(response_headers["Location"]).query)
    assert callback == {"state": ["client-state"], "error": ["access_denied"]}
    assert (await decide(browser, fields, headers))[0] == 403


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
