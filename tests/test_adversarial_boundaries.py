"""Adversarial interleavings that must preserve authorization boundaries."""

import json
import re
from urllib.parse import urlencode

import pytest
from test_authorization import authority as authority
from test_owner_passkeys import _registration_response
from test_owner_passkeys import setup as setup


@pytest.mark.asyncio
async def test_atomic_registration_does_not_use_split_ticket_consumption(
    setup, monkeypatch,
):
    """Detect reintroduction of the split path, including its owner reset gap."""
    browser, passkeys = setup
    ticket = passkeys.issue_local_ticket("owner-password")
    status, page, response_headers = await browser.owner_passkey(
        "GET", {}, b"", urlencode({"ticket": ticket}),
    )
    assert status == 200
    identity = next(reversed(browser.registrations))
    _, _, credential = _registration_response(browser.registrations[identity].challenge)
    csrf = re.search(rb"name=csrf value='([^']+)'", page)
    assert csrf is not None
    headers = {
        "origin": "https://computer.example",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": response_headers["Set-Cookie"].split(";", 1)[0],
    }
    original = passkeys.consume_ticket
    split_calls = 0

    def reset_after_consume(value):
        nonlocal split_calls
        split_calls += 1
        consumed = original(value)
        if consumed:
            passkeys.clear()
        return consumed

    monkeypatch.setattr(browser.passkeys, "consume_ticket", reset_after_consume)
    result = await browser.owner_passkey("POST", headers, urlencode({
        "request_id": identity,
        "csrf": csrf.group(1).decode(),
        "ticket": ticket,
        "response": json.dumps(credential),
        "label": "Racing key",
    }).encode())
    # The legacy split path invokes consume_ticket and would recreate the key
    # after reset. Atomic enrollment never enters that gap.
    assert result[0] == 200
    assert len(passkeys.list()) == 1
    assert split_calls == 0


@pytest.mark.asyncio
async def test_registration_storage_failure_consumes_ticket(setup, monkeypatch):
    browser, passkeys = setup
    ticket = passkeys.issue_local_ticket("owner-password")
    status, page, response_headers = await browser.owner_passkey(
        "GET", {}, b"", urlencode({"ticket": ticket}),
    )
    assert status == 200
    identity = next(reversed(browser.registrations))
    _, _, credential = _registration_response(browser.registrations[identity].challenge)
    csrf = re.search(rb"name=csrf value='([^']+)'", page)
    assert csrf is not None
    headers = {
        "origin": "https://computer.example",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": response_headers["Set-Cookie"].split(";", 1)[0],
    }
    form = urlencode({
        "request_id": identity, "csrf": csrf.group(1).decode(), "ticket": ticket,
        "response": json.dumps(credential), "label": "Retry key",
    }).encode()
    original = browser.passkeys._write
    calls = 0

    def fail_once(records):
        nonlocal calls
        calls += 1
        if calls == 1:
            from anywhere_computer.client_tokens import CredentialStoreUnavailable
            raise CredentialStoreUnavailable("Synthetic vault outage")
        original(records)

    monkeypatch.setattr(browser.passkeys, "_write", fail_once)
    failed = await browser.owner_passkey("POST", headers, form)
    assert failed[0] == 503
    assert not passkeys.ticket_valid(ticket)
    retried = await browser.owner_passkey("POST", headers, form)
    assert retried[0] == 403
    assert not passkeys.ticket_valid(ticket)
    assert passkeys.list() == []
