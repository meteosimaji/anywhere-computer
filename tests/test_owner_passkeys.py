import asyncio
import base64
import hashlib
import json
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from test_authorization import REDIRECT, RESOURCE, VERIFIER, pkce_s256
from test_authorization import authority as authority
from test_client_tokens import MemoryVault

from anywhere_computer import owner_passkeys as passkey_module
from anywhere_computer.browser_authorization import BrowserAuthorization
from anywhere_computer.client_tokens import ClientCredentialError
from anywhere_computer.credentials import SERVICE
from anywhere_computer.http_service import (
    configure_http,
    enable_http_device,
    http_authorization_status,
    reset_http_owner_password,
)
from anywhere_computer.locking import ProcessLock
from anywhere_computer.owner_credentials import OwnerCredentials
from anywhere_computer.owner_passkeys import (
    OwnerPasskeys,
    PasskeyEnrollmentLimitReached,
    PasskeyRecord,
    _RedeemedTicket,
)


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def test_parallel_assertions_cannot_reuse_one_counter(setup):
    _, passkeys = setup
    record = PasskeyRecord(credential_id=b64(secrets.token_bytes(32)),
                           public_key=b64(secrets.token_bytes(90)),
                           sign_count=0, label="counter key")
    passkeys.register(record)
    entered = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []

    def first_verify(current: PasskeyRecord) -> int:
        assert current.sign_count == 0
        entered.set()
        assert release.wait(5)
        return 1

    def verify_same_assertion(current: PasskeyRecord) -> int:
        if current.sign_count >= 1:
            raise ValueError("counter replay")
        return 1

    def run_first() -> None:
        assert passkeys.verify_and_update_counter(record.credential_id, first_verify)
        outcomes.append("accepted")

    def run_second() -> None:
        try:
            passkeys.verify_and_update_counter(record.credential_id,
                                               verify_same_assertion)
        except (ValueError, TimeoutError):
            outcomes.append("rejected")

    first = threading.Thread(target=run_first)
    second = threading.Thread(target=run_second)
    first.start()
    assert entered.wait(5)
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert sorted(outcomes) == ["accepted", "rejected"]
    assert passkeys.find(record.credential_id).sign_count == 1
    with pytest.raises(ValueError, match="counter replay"):
        passkeys.verify_and_update_counter(record.credential_id,
                                           verify_same_assertion)


def test_passkey_password_checks_hold_reset_lock(setup, monkeypatch):
    _, passkeys = setup
    def verify_under_reset_lock(_password):
        with pytest.raises(TimeoutError, match="owner-reset.lock"):
            with ProcessLock(passkeys.credentials.directory / "owner-reset.lock"):
                pass
        return True

    monkeypatch.setattr(passkeys.credentials, "verify", verify_under_reset_lock)
    ticket = passkeys.issue_local_ticket("owner-password")
    assert passkeys.ticket_valid(ticket)
    with pytest.raises(ValueError, match="not enrolled"):
        passkeys.remove_with_password("absent", "owner-password")


def test_password_change_invalidates_old_enrollment_ticket(tmp_path, fast_owner_derivation):
    owner = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    owner.initialize("old owner password")
    passkeys = OwnerPasskeys(owner, device="device")
    old_ticket = passkeys.issue_local_ticket("old owner password")
    assert passkeys.ticket_valid(old_ticket)
    owner.change_password("old owner password", "new owner password")
    assert not passkeys.ticket_valid(old_ticket)
    assert not passkeys.consume_ticket(old_ticket)
    record = PasskeyRecord(credential_id=b64(secrets.token_bytes(32)),
                          public_key=b64(secrets.token_bytes(90)),
                          sign_count=0, label="New key")
    assert not passkeys.register_with_ticket(old_ticket, record)
    assert passkeys.list() == []
    new_ticket = passkeys.issue_local_ticket("new owner password")
    assert passkeys.register_with_ticket(new_ticket, record)
    assert not passkeys.ticket_valid(new_ticket)


def test_failed_enrollment_cannot_reuse_ticket_after_vault_error(setup, monkeypatch):
    _, passkeys = setup
    ticket = passkeys.issue_local_ticket("owner-password")
    first = PasskeyRecord(credential_id=b64(secrets.token_bytes(32)),
                          public_key=b64(secrets.token_bytes(90)),
                          sign_count=0, label="First key")
    second = first.model_copy(update={"credential_id": b64(secrets.token_bytes(32))})
    original_write = passkeys._write

    def write_then_fail(records):
        original_write(records)
        raise ClientCredentialError("simulated failure after vault commit")

    monkeypatch.setattr(passkeys, "_write", write_then_fail)
    with pytest.raises(ClientCredentialError, match="simulated failure"):
        passkeys.register_with_ticket(ticket, first)
    monkeypatch.setattr(passkeys, "_write", original_write)
    assert passkeys.find(first.credential_id) is not None
    assert not passkeys.ticket_valid(ticket)
    assert not passkeys.register_with_ticket(ticket, second)
    assert passkeys.find(second.credential_id) is None


def test_reappearing_ticket_cannot_enroll_again_after_vault_commit(setup, monkeypatch):
    _, passkeys = setup
    ticket = passkeys.issue_local_ticket("owner-password")
    ticket_bytes = passkeys.ticket_path.read_bytes()
    first = PasskeyRecord(credential_id=b64(secrets.token_bytes(32)),
                          public_key=b64(secrets.token_bytes(90)),
                          sign_count=0, label="First key")
    second = first.model_copy(update={"credential_id": b64(secrets.token_bytes(32))})
    assert passkeys.register_with_ticket(ticket, first)
    # Model a crash in which the directory deletion was not durable but the
    # native credential-store write survived.
    passkeys.ticket_path.write_bytes(ticket_bytes)
    assert not passkeys.ticket_valid(ticket)
    assert not passkeys.register_with_ticket(ticket, second)
    passkeys.remove(first.credential_id)
    assert not passkeys.ticket_valid(ticket)
    assert not passkeys.register_with_ticket(ticket, second)
    assert passkeys.list() == []
    later_ticket = passkeys.issue_local_ticket("owner-password")
    assert passkeys.register_with_ticket(later_ticket, second)
    current_time = time.time()
    with monkeypatch.context() as later_clock:
        later_clock.setattr(passkey_module.time, "time", lambda: current_time + 1000)
        newer_ticket = passkeys.issue_local_ticket("owner-password")
        newer_key = first.model_copy(update={"credential_id": b64(secrets.token_bytes(32))})
        assert passkeys.register_with_ticket(newer_ticket, newer_key)
    assert len(passkeys._read().redeemed_tickets) == 3
    passkeys.ticket_path.write_bytes(ticket_bytes)
    third = first.model_copy(update={"credential_id": b64(secrets.token_bytes(32))})
    assert not passkeys.ticket_valid(ticket)
    assert not passkeys.register_with_ticket(ticket, third)
    assert passkeys.find(third.credential_id) is None


@pytest.mark.asyncio
async def test_registration_reports_unavailable_when_passkey_store_cannot_be_read(
        setup, monkeypatch):
    browser, passkeys = setup
    ticket = passkeys.issue_local_ticket("owner-password")

    def unavailable():
        raise ClientCredentialError("synthetic passkey store failure")

    monkeypatch.setattr(browser.passkeys, "_read", unavailable)
    status, body, _ = await browser.owner_passkey(
        "GET", {}, b"", urlencode({"ticket": ticket}))
    assert status == 503
    assert body is not None and b"Passkey storage is unavailable" in body


def test_redeemed_ticket_limit_fails_before_new_registration(setup):
    _, passkeys = setup
    current = passkeys._read()
    passkeys._write(current.model_copy(update={"redeemed_tickets": [
        _RedeemedTicket(digest=f"{index:064x}", expires=time.time() + 300)
        for index in range(128)
    ]}))
    ticket = passkeys.issue_local_ticket("owner-password")
    record = PasskeyRecord(credential_id=b64(secrets.token_bytes(32)),
                           public_key=b64(secrets.token_bytes(90)),
                           sign_count=0, label="Over limit")
    with pytest.raises(PasskeyEnrollmentLimitReached, match="reset the owner"):
        passkeys.register_with_ticket(ticket, record)
    assert passkeys.ticket_valid(ticket)
    assert passkeys.list() == []


def test_credential_limit_preserves_existing_passkeys_and_ticket(setup):
    _, passkeys = setup
    records = [PasskeyRecord(credential_id=b64(index.to_bytes(32, "big")),
                             public_key=b64(secrets.token_bytes(90)),
                             sign_count=0, label=f"Key {index}")
               for index in range(20)]
    for record in records:
        passkeys.register(record)
    extra = PasskeyRecord(credential_id=b64((20).to_bytes(32, "big")),
                          public_key=b64(secrets.token_bytes(90)),
                          sign_count=0, label="Extra key")
    ticket = passkeys.issue_local_ticket("owner-password")
    with pytest.raises(PasskeyEnrollmentLimitReached):
        passkeys.register_with_ticket(ticket, extra)
    assert passkeys.ticket_valid(ticket)
    with pytest.raises(PasskeyEnrollmentLimitReached):
        passkeys.register(extra)
    assert len(passkeys.list()) == 20
    assert passkeys.find(extra.credential_id) is None
    for record in records:
        assert passkeys.verify_and_update_counter(
            record.credential_id, lambda _current: 1)
        assert passkeys.find(record.credential_id).sign_count == 1


@pytest.mark.asyncio
async def test_full_passkey_store_rejects_registration_before_challenge(setup):
    browser, passkeys = setup
    for index in range(20):
        passkeys.register(PasskeyRecord(
            credential_id=b64(index.to_bytes(32, "big")),
            public_key=b64(secrets.token_bytes(90)),
            sign_count=0, label=f"Key {index}",
        ))
    ticket = passkeys.issue_local_ticket("owner-password")
    status, page, _ = await browser.owner_passkey(
        "GET", {}, b"", urlencode({"ticket": ticket}))
    assert status == 409
    assert page is not None and b"Passkey enrollment limit reached" in page
    assert b"data-options" not in page
    assert browser.registrations == {}
    assert passkeys.ticket_valid(ticket)
    assert len(passkeys.list()) == 20


@pytest.mark.asyncio
async def test_registration_limit_has_owner_action_message(setup, monkeypatch):
    browser, _ = setup

    def limit(_query):
        raise PasskeyEnrollmentLimitReached("synthetic limit")

    monkeypatch.setattr(browser, "_registration_begin", limit)
    status, body, _ = await browser.owner_passkey("GET", {}, b"", "ticket=fixture")
    assert status == 409
    assert body is not None and b"Reset the owner locally" in body


@pytest.fixture
def setup(authority, tmp_path, fast_owner_derivation):
    vault = MemoryVault()
    owner = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=vault)
    owner.initialize("owner-password")
    browser = BrowserAuthorization(authority, owner, device="device")
    return browser, OwnerPasskeys(owner, device="device")


def _key():
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_numbers()
    cose = cbor2.dumps({
        1: 2, 3: -7, -1: 1,
        -2: public.x.to_bytes(32, "big"), -3: public.y.to_bytes(32, "big"),
    })
    return private, cose


def _client_data(kind: str, challenge: bytes, origin: str) -> bytes:
    return json.dumps({"type": kind, "challenge": b64(challenge), "origin": origin}).encode()


def _registration_response(
    challenge, *, origin="https://computer.example", rp_id="computer.example",
    uv=True, synced=False,
):
    private, cose = _key()
    credential_id = secrets.token_bytes(32)
    flags = 0x01 | 0x40 | (0x04 if uv else 0) | (0x08 | 0x10 if synced else 0)
    auth_data = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags])
                 + (0).to_bytes(4, "big") + bytes(16)
                 + len(credential_id).to_bytes(2, "big") + credential_id + cose)
    attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
    return private, credential_id, {
        "id": b64(credential_id), "rawId": b64(credential_id), "type": "public-key",
        "response": {
            "clientDataJSON": b64(_client_data("webauthn.create", challenge, origin)),
            "attestationObject": b64(attestation),
        },
    }


def _assertion(private, credential_id, challenge, *, origin="https://computer.example",
               rp_id="computer.example", uv=True, synced=False, count=0):
    client = _client_data("webauthn.get", challenge, origin)
    flags = 0x01 | (0x04 if uv else 0) | (0x08 | 0x10 if synced else 0)
    auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + count.to_bytes(4, "big")
    signature = private.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
    return {
        "id": b64(credential_id), "rawId": b64(credential_id), "type": "public-key",
        "response": {"clientDataJSON": b64(client), "authenticatorData": b64(auth_data),
                     "signature": b64(signature), "userHandle": None},
    }


async def _enroll(browser, passkeys, *, origin="https://computer.example",
                  rp_id="computer.example", uv=True, synced=False):
    ticket = passkeys.issue_local_ticket("owner-password")
    status, body, headers = await browser.owner_passkey(
        "GET", {}, b"", urlencode({"ticket": ticket})
    )
    assert status == 200 and b"Register an owner passkey" in body
    identity = next(reversed(browser.registrations))
    record = browser.registrations[identity]
    private, credential_id, response = _registration_response(
        record.challenge, origin=origin, rp_id=rp_id, uv=uv, synced=synced
    )
    csrf = __import__("re").search(rb"name=csrf value='([^']+)'", body).group(1).decode()
    form = urlencode({"request_id": identity, "csrf": csrf, "ticket": ticket,
                      "response": json.dumps(response), "label": "Laptop"}).encode()
    request_headers = {
        "origin": "https://computer.example",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": headers["Set-Cookie"].split(";", 1)[0],
    }
    result = await browser.owner_passkey("POST", request_headers, form)
    return result, private, credential_id, ticket, request_headers, form


async def _consent(browser):
    query = urlencode({
        "response_type": "code", "client_id": "client", "redirect_uri": REDIRECT,
        "resource": RESOURCE, "scope": "files_read", "state": "client-state",
        "code_challenge": pkce_s256(VERIFIER), "code_challenge_method": "S256",
    })
    status, page, headers = await browser.authorize("GET", {}, b"", query)
    assert status == 200 and b"Allow with passkey" in page
    import re
    fields = dict(re.findall(rb"name=(request_id|csrf) value='([^']+)'", page))
    identity = fields[b"request_id"].decode()
    request_headers = {"origin": "https://computer.example",
                       "content-type": "application/x-www-form-urlencoded",
                       "cookie": headers["Set-Cookie"].split(";", 1)[0]}
    return (
        {key.decode(): value.decode() for key, value in fields.items()},
        request_headers,
        identity,
    )


async def _approve(browser, fields, headers, assertion):
    return await browser.authorize("POST", headers, urlencode({
        **fields, "passkey": "yes", "assertion": json.dumps(assertion),
    }).encode())


async def test_local_registration_and_passkey_consent(setup):
    browser, passkeys = setup
    registered, private, credential_id, ticket, headers, form = await _enroll(browser, passkeys)
    assert registered[0] == 200
    assert len(passkeys.list()) == 1
    assert not passkeys.ticket_valid(ticket)
    assert (await browser.owner_passkey("POST", headers, form))[0] == 403
    fields, request_headers, identity = await _consent(browser)
    assertion = _assertion(private, credential_id, browser.pending[identity].passkey_challenge)
    approved = await _approve(browser, fields, request_headers, assertion)
    assert approved[0] == 303
    assert "code" in parse_qs(urlsplit(approved[2]["Location"]).query)
    assert (await _approve(browser, fields, request_headers, assertion))[0] == 403


async def test_passkey_assertion_is_bound_to_one_pending_request_and_device(setup):
    browser, passkeys = setup
    registered, private, credential_id, *_ = await _enroll(browser, passkeys)
    assert registered[0] == 200
    first_fields, _, first_identity = await _consent(browser)
    second_fields, second_headers, second_identity = await _consent(browser)
    assert first_fields["request_id"] != second_fields["request_id"]
    assertion = _assertion(
        private, credential_id, browser.pending[first_identity].passkey_challenge
    )
    result = await _approve(browser, second_fields, second_headers, assertion)
    assert result[0] == 403 and "Location" not in result[2]
    assert OwnerPasskeys(browser.credentials, device="another-device").find(
        b64(credential_id)
    ) is None


@pytest.mark.parametrize("failure", ["origin", "rp_id", "uv", "expired", "revoke", "counter"])
async def test_passkey_rejects_invalid_or_revoked_consent(setup, authority, failure):
    browser, passkeys = setup
    registered, private, credential_id, *_ = await _enroll(browser, passkeys)
    assert registered[0] == 200
    fields, headers, identity = await _consent(browser)
    consent = browser.pending[identity]
    kwargs = {}
    if failure == "origin":
        kwargs["origin"] = "https://other.example"
    elif failure == "rp_id":
        kwargs["rp_id"] = "other.example"
    elif failure == "uv":
        kwargs["uv"] = False
    elif failure == "counter":
        kwargs["count"] = 1
        # First valid assertion advances the single-device counter.
        first = _assertion(private, credential_id, consent.passkey_challenge, count=1)
        assert (await _approve(browser, fields, headers, first))[0] == 303
        fields, headers, identity = await _consent(browser)
        consent = browser.pending[identity]
    elif failure == "expired":
        browser.pending[identity] = __import__("dataclasses").replace(
            consent, expires=time.monotonic() - 1
        )
    elif failure == "revoke":
        authority.revoke_device(owner="owner", device="device")
    assertion = _assertion(private, credential_id, consent.passkey_challenge, **kwargs)
    result = await _approve(browser, fields, headers, assertion)
    assert result[0] in (400, 403)
    assert "Location" not in result[2]


@pytest.mark.parametrize("failure", ["origin", "rp_id", "uv"])
async def test_registration_validates_origin_rp_and_user_verification(setup, failure):
    browser, passkeys = setup
    kwargs = {"origin": "https://other.example"} if failure == "origin" else {}
    if failure == "rp_id":
        kwargs["rp_id"] = "other.example"
    if failure == "uv":
        kwargs["uv"] = False
    result, *_ = await _enroll(browser, passkeys, **kwargs)
    assert result[0] == 403
    assert passkeys.list() == []


async def test_synced_passkey_counter_can_be_nonmonotonic_across_devices(setup):
    browser, passkeys = setup
    registered, private, credential_id, *_ = await _enroll(browser, passkeys, synced=True)
    assert registered[0] == 200
    for count in (8, 2, 0):
        fields, headers, identity = await _consent(browser)
        assertion = _assertion(private, credential_id, browser.pending[identity].passkey_challenge,
                               synced=True, count=count)
        assert (await _approve(browser, fields, headers, assertion))[0] == 303


async def test_public_authorize_cannot_issue_enrollment_ticket(setup):
    browser, passkeys = setup
    assert (await browser.owner_passkey("GET", {}, b"", "ticket=untrusted"))[0] == 403
    assert passkeys.list() == []


@pytest.mark.parametrize("failure", ["origin", "cookie", "csrf", "expired"])
async def test_registration_requires_bound_browser_and_unexpired_challenge(
    setup, failure,
):
    browser, passkeys = setup
    ticket = passkeys.issue_local_ticket("owner-password")
    status, page, response_headers = await browser.owner_passkey(
        "GET", {}, b"", urlencode({"ticket": ticket})
    )
    assert status == 200
    identity = next(reversed(browser.registrations))
    record = browser.registrations[identity]
    _, _, credential = _registration_response(record.challenge)
    import re
    csrf = re.search(rb"name=csrf value='([^']+)'", page).group(1).decode()
    headers = {
        "origin": "https://computer.example",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": response_headers["Set-Cookie"].split(";", 1)[0],
    }
    if failure == "origin":
        headers["origin"] = "https://other.example"
    elif failure == "cookie":
        headers["cookie"] = "invalid"
    elif failure == "csrf":
        csrf = "invalid"
    else:
        browser.registrations[identity] = __import__("dataclasses").replace(
            record, expires=time.monotonic() - 1
        )
    result = await browser.owner_passkey("POST", headers, urlencode({
        "request_id": identity, "csrf": csrf, "ticket": ticket,
        "response": json.dumps(credential), "label": "Laptop",
    }).encode())
    assert result[0] == 403
    assert passkeys.list() == []


async def test_passkey_approval_racing_device_revocation_cannot_issue_code(
    setup, authority, monkeypatch,
):
    browser, passkeys = setup
    registered, private, credential_id, *_ = await _enroll(browser, passkeys)
    assert registered[0] == 200
    fields, headers, identity = await _consent(browser)
    assertion = _assertion(private, credential_id, browser.pending[identity].passkey_challenge)
    import anywhere_computer.browser_authorization as module

    original = module.verify_authentication_response
    entered, release = threading.Event(), threading.Event()

    def paused_verify(**kwargs):
        entered.set()
        release.wait(30)
        return original(**kwargs)

    monkeypatch.setattr(module, "verify_authentication_response", paused_verify)
    pending = asyncio.create_task(_approve(browser, fields, headers, assertion))
    try:
        assert await asyncio.to_thread(entered.wait, 30)
        authority.revoke_device(owner="owner", device="device")
        assert authority.enable_device(owner="owner", device="device")
    finally:
        release.set()
    result = await pending
    assert result[0] == 400 and "Location" not in result[2]
    assert authority.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0


async def test_passkey_approval_racing_owner_reset_cannot_issue_code(
    setup, authority, monkeypatch,
):
    browser, passkeys = setup
    registered, private, credential_id, *_ = await _enroll(browser, passkeys)
    assert registered[0] == 200
    fields, headers, identity = await _consent(browser)
    assertion = _assertion(private, credential_id, browser.pending[identity].passkey_challenge)
    entered, release = threading.Event(), threading.Event()
    original = browser.passkeys.verify_and_update_counter

    def paused_update(credential_id: str, verify) -> bool:
        accepted = original(credential_id, verify)
        entered.set()
        release.wait(30)
        return accepted

    monkeypatch.setattr(browser.passkeys, "verify_and_update_counter", paused_update)
    pending = asyncio.create_task(_approve(browser, fields, headers, assertion))
    try:
        assert await asyncio.to_thread(entered.wait, 30)
        browser.credentials.reset_password(
            "new owner password",
            lambda: authority.revoke_device(owner="owner", device="device"),
        )
        passkeys.clear()
    finally:
        release.set()
    result = await pending
    assert result[0] == 403 and "Location" not in result[2]
    assert passkeys.list() == []
    assert authority.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0


async def test_passkey_removed_after_assertion_cannot_approve_consent(
    setup, authority, monkeypatch,
):
    browser, passkeys = setup
    registered, private, credential_id, *_ = await _enroll(browser, passkeys)
    assert registered[0] == 200
    fields, headers, identity = await _consent(browser)
    assertion = _assertion(private, credential_id, browser.pending[identity].passkey_challenge)
    entered, release = threading.Event(), threading.Event()
    original = browser.passkeys.verify_and_update_counter

    def paused_update(credential: str, verify) -> bool:
        accepted = original(credential, verify)
        entered.set()
        assert release.wait(30)
        return accepted

    monkeypatch.setattr(browser.passkeys, "verify_and_update_counter", paused_update)
    pending = asyncio.create_task(_approve(browser, fields, headers, assertion))
    try:
        assert await asyncio.to_thread(entered.wait, 30)
        passkeys.remove_with_password(b64(credential_id), "owner-password")
    finally:
        release.set()
    result = await pending
    assert result[0] == 403 and "Location" not in result[2]
    assert passkeys.list() == []
    assert authority.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0


async def test_passkey_lock_contention_fails_without_stalling_consent_loop(
    setup, authority, monkeypatch,
):
    browser, passkeys = setup
    registered, private, credential_id, *_ = await _enroll(browser, passkeys)
    assert registered[0] == 200
    fields, headers, identity = await _consent(browser)
    assertion = _assertion(private, credential_id, browser.pending[identity].passkey_challenge)
    entered, release = threading.Event(), threading.Event()
    original = browser.passkeys.verify_and_update_counter

    def paused_update(credential: str, verify) -> bool:
        accepted = original(credential, verify)
        entered.set()
        assert release.wait(30)
        return accepted

    monkeypatch.setattr(browser.passkeys, "verify_and_update_counter", paused_update)
    pending = asyncio.create_task(_approve(browser, fields, headers, assertion))
    assert await asyncio.to_thread(entered.wait, 30)
    with ProcessLock(passkeys.lock_path):
        release.set()
        started = time.monotonic()
        result = await asyncio.wait_for(pending, 5)
        assert time.monotonic() - started < 1
    assert result[0] == 503 and "Location" not in result[2]
    assert authority.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0


async def test_corrupt_optional_passkey_store_preserves_password_consent(setup):
    browser, passkeys = setup
    browser.credentials.vault.data[SERVICE, passkeys.account] = "corrupt"
    query = urlencode({
        "response_type": "code", "client_id": "client", "redirect_uri": REDIRECT,
        "resource": RESOURCE, "scope": "files_read", "state": "client-state",
        "code_challenge": pkce_s256(VERIFIER), "code_challenge_method": "S256",
    })
    status, page, headers = await browser.authorize("GET", {}, b"", query)
    assert status == 200 and b"Anywhere Computer owner password" in page
    assert b"Allow with passkey" not in page
    import re
    fields = dict(re.findall(rb"name=(request_id|csrf) value='([^']+)'", page))
    result = await browser.authorize("POST", {
        "origin": "https://computer.example",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": headers["Set-Cookie"].split(";", 1)[0],
    }, urlencode({**{key.decode(): value.decode() for key, value in fields.items()},
                  "approve": "yes", "password": "owner-password"}).encode())
    assert result[0] == 303


async def test_local_owner_reset_revokes_enrolled_passkeys_and_grants(
    tmp_path, unused_tcp_port, monkeypatch, fast_owner_derivation,
):
    config = await configure_http(
        tmp_path, resource=RESOURCE, owner="owner", client="native",
        port=unused_tcp_port, scopes=frozenset({"files_read"}),
        redirects=frozenset({REDIRECT}),
    )
    vault = MemoryVault()
    owner = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=vault)
    owner.initialize("old owner password")
    passkeys = OwnerPasskeys(owner, device=config.device)
    passkeys.register(PasskeyRecord(
        credential_id=b64(secrets.token_bytes(32)),
        public_key=b64(secrets.token_bytes(90)),
        sign_count=0, label="Lost key",
    ))
    used_ticket = passkeys.issue_local_ticket("old owner password")
    assert passkeys.register_with_ticket(used_ticket, PasskeyRecord(
        credential_id=b64(secrets.token_bytes(32)),
        public_key=b64(secrets.token_bytes(90)),
        sign_count=0, label="Second key",
    ))
    assert len(passkeys._read().redeemed_tickets) == 1
    ticket = passkeys.issue_local_ticket("old owner password")
    monkeypatch.setattr("anywhere_computer.owner_credentials.secure_backend", lambda: vault)
    reset_http_owner_password(tmp_path, "new owner password")
    assert not http_authorization_status(tmp_path)["device_enabled"]
    assert passkeys.list() == []
    assert passkeys._read().redeemed_tickets == []
    assert not passkeys.ticket_valid(ticket)


async def test_failed_passkey_clear_blocks_owner_reset_reenable_until_retry(
    tmp_path, unused_tcp_port, monkeypatch, fast_owner_derivation,
):
    config = await configure_http(
        tmp_path, resource=RESOURCE, owner="owner", client="native",
        port=unused_tcp_port, scopes=frozenset({"files_read"}),
        redirects=frozenset({REDIRECT}),
    )
    vault = MemoryVault()
    owner = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=vault)
    owner.initialize("old owner password")
    passkeys = OwnerPasskeys(owner, device=config.device)
    old_key = PasskeyRecord(
        credential_id=b64(secrets.token_bytes(32)),
        public_key=b64(secrets.token_bytes(90)),
        sign_count=0, label="Lost key",
    )
    passkeys.register(old_key)
    original_write = vault.set_password

    def reject_clear(service, account, value):
        if account == passkeys.account and '"credentials":[]' in value:
            raise RuntimeError("Synthetic passkey clear failed")
        original_write(service, account, value)

    monkeypatch.setattr(vault, "set_password", reject_clear)
    monkeypatch.setattr("anywhere_computer.owner_credentials.secure_backend", lambda: vault)
    with pytest.raises(ClientCredentialError, match="not confirmed"):
        reset_http_owner_password(tmp_path, "new owner password")
    assert not http_authorization_status(tmp_path)["device_enabled"]
    assert [item.credential_id for item in passkeys.list()] == [old_key.credential_id]
    with pytest.raises(ValueError, match="incomplete"):
        enable_http_device(tmp_path)
    monkeypatch.setattr(vault, "set_password", original_write)
    reset_http_owner_password(tmp_path, "newer owner password")
    assert passkeys.list() == []
    assert enable_http_device(tmp_path)
