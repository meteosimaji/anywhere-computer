"""First owner setup with a platform passkey and no password verifier."""

import base64
import json
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from test_authorization import REDIRECT, RESOURCE, VERIFIER, pkce_s256
from test_authorization import authority as authority
from test_browser_authorization import begin
from test_client_tokens import MemoryVault
from test_http_service import SCOPES, _issued_reset_token, _verify_reset_token
from test_owner_passkeys import _assertion, _enroll, b64

from anywhere_computer import cli
from anywhere_computer.browser_authorization import BrowserAuthorization
from anywhere_computer.client_tokens import ClientCredentialError
from anywhere_computer.http_service import (
    begin_http_owner_passkey,
    configure_http,
    http_authorization_status,
    http_service,
    reset_http_owner_passkey,
)
from anywhere_computer.owner_credentials import OwnerCredentials
from anywhere_computer.owner_passkeys import OwnerPasskeys, PasskeyRecord


def _owner(tmp_path, vault):
    return OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=vault)


def _record():
    return PasskeyRecord(
        credential_id=b64(secrets.token_bytes(32)),
        public_key=b64(secrets.token_bytes(90)),
        sign_count=0,
        label="Windows Hello",
    )


def test_passkey_only_binding_cannot_verify_password_or_replace_owner(tmp_path):
    owner = _owner(tmp_path, MemoryVault())
    owner.initialize_passkey_only()
    assert owner.is_passkey_only() and owner.is_initialized()
    assert not owner.verify("any password")
    with pytest.raises(ClientCredentialError, match="already exist"):
        owner.initialize("synthetic owner password")
    with pytest.raises(ClientCredentialError, match="already exist"):
        owner.initialize_passkey_only()


def test_first_ticket_is_one_use_and_no_second_key_can_be_added(tmp_path):
    owner = _owner(tmp_path, MemoryVault())
    owner.initialize_passkey_only()
    passkeys = OwnerPasskeys(owner, device="device")
    ticket = passkeys.issue_initial_local_ticket()
    assert passkeys.ticket_valid(ticket)
    assert passkeys.register_with_ticket(ticket, _record())
    assert not passkeys.ticket_valid(ticket)
    assert not passkeys.register_with_ticket(ticket, _record())
    with pytest.raises(ValueError, match="already enrolled"):
        passkeys.issue_initial_local_ticket()
    with pytest.raises(ValueError, match="incorrect"):
        passkeys.issue_local_ticket("synthetic owner password")


@pytest.mark.asyncio
async def test_windows_hello_registration_and_consent(authority, tmp_path):
    owner = _owner(tmp_path, MemoryVault())
    owner.initialize_passkey_only()
    browser = BrowserAuthorization(authority, owner, device="device")
    passkeys = browser.passkeys
    ticket = passkeys.issue_initial_local_ticket()
    status, page, _ = await browser.owner_passkey("GET", {}, b"", urlencode({"ticket": ticket}))
    assert status == 200 and page is not None
    assert b"Choose Windows Hello" in page
    assert b"<div id=phone-choice>" not in page
    encoded = __import__("re").search(rb"data-options='([^']+)'", page).group(1)
    options = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
    assert options["authenticatorSelection"]["authenticatorAttachment"] == "platform"
    assert options["authenticatorSelection"]["userVerification"] == "required"

    enrolled, private, credential_id, _, _, _ = await _enroll(browser, passkeys, ticket=ticket)
    assert enrolled[0] == 200 and len(passkeys.list()) == 1
    fields, headers = await begin(browser)
    pending = browser.pending[fields["request_id"]]
    page = browser._page(fields["request_id"], pending, fields["csrf"]).decode()
    assert "id=password" not in page and "Allow with passkey" in page
    rejected = await browser.authorize(
        "POST", headers, urlencode({**fields, "approve": "yes", "password": "guess"}).encode()
    )
    assert rejected[0] == 403
    assert b"Use the enrolled owner passkey" in rejected[1]
    assertion = _assertion(private, credential_id, pending.passkey_challenge, count=1)
    approved = await browser.authorize(
        "POST", headers, urlencode({**fields, "passkey": "yes",
                                    "assertion": json.dumps(assertion)}).encode()
    )
    assert approved[0] == 303


@pytest.mark.asyncio
async def test_wrong_origin_cannot_enroll_initial_key(authority, tmp_path):
    owner = _owner(tmp_path, MemoryVault())
    owner.initialize_passkey_only()
    browser = BrowserAuthorization(authority, owner, device="device")
    ticket = browser.passkeys.issue_initial_local_ticket()
    rejected, _, _, _, _, _ = await _enroll(
        browser, browser.passkeys, ticket=ticket, origin="https://other.example"
    )
    assert rejected[0] == 403
    assert browser.passkeys.list() == []
    assert browser.passkeys.ticket_valid(ticket)


@pytest.mark.asyncio
async def test_local_bootstrap_rejects_old_grants_and_offline_reset_revokes_them(
    tmp_path, unused_tcp_port, monkeypatch,
):
    config = await configure_http(
        tmp_path, resource=RESOURCE, owner="owner", client="native",
        port=unused_tcp_port, scopes=SCOPES,
        redirects=frozenset({"https://client.example/callback"}),
    )
    vault = MemoryVault()
    monkeypatch.setattr("anywhere_computer.owner_credentials.secure_backend", lambda: vault)
    first = begin_http_owner_passkey(tmp_path)
    owner = _owner(tmp_path, vault)
    assert owner.is_passkey_only()
    passkeys = OwnerPasskeys(owner, device=config.device)
    assert passkeys.register_with_ticket(first, _record())
    token = _issued_reset_token(tmp_path, config)
    assert _verify_reset_token(tmp_path, config, token) is not None
    with pytest.raises(ValueError, match="Existing grants"):
        begin_http_owner_passkey(tmp_path)

    reset_http_owner_passkey(tmp_path)
    assert not http_authorization_status(tmp_path)["device_enabled"]
    assert _verify_reset_token(tmp_path, config, token) is None
    assert passkeys.list() == []
    assert not passkeys.ticket_valid(first)
    replacement = begin_http_owner_passkey(tmp_path)
    assert replacement != first and passkeys.ticket_valid(replacement)


@pytest.mark.asyncio
async def test_http_service_starts_without_password_but_cannot_approve_without_key(
    tmp_path, unused_tcp_port,
):
    config = await configure_http(
        tmp_path, resource=RESOURCE, owner="owner", client="native",
        port=unused_tcp_port, scopes=frozenset({"files_read"}),
        redirects=frozenset({REDIRECT}),
    )
    owner = _owner(tmp_path, MemoryVault())
    owner.initialize_passkey_only()
    async with http_service(tmp_path, credentials=owner):
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{config.port}", trust_env=False,
        ) as http:
            response = await http.get("/authorize", params={
                "response_type": "code", "client_id": "native", "redirect_uri": REDIRECT,
                "resource": RESOURCE, "scope": "files_read", "state": "client-state",
                "code_challenge": pkce_s256(VERIFIER), "code_challenge_method": "S256",
            })
            assert response.status_code == 200
            assert "id=password" not in response.text
            assert "<button class=btn type=submit name=approve" not in response.text
            assert "Allow with passkey" not in response.text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only CLI")
def test_windows_hello_cli_refuses_piped_bootstrap(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["anywhere", "owner-passkey-init"])
    monkeypatch.setattr(cli, "has_interactive_input", lambda: False)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert "interactive local terminal" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only CLI")
async def test_windows_hello_cli_creates_private_binding_and_registration_link(
    tmp_path, unused_tcp_port, monkeypatch, capsys,
):
    config = await configure_http(
        tmp_path, resource=RESOURCE, owner="owner", client="native",
        port=unused_tcp_port, scopes=frozenset({"files_read"}),
        redirects=frozenset({REDIRECT}),
    )
    vault = MemoryVault()
    monkeypatch.setattr("anywhere_computer.owner_credentials.secure_backend", lambda: vault)
    monkeypatch.setattr(cli, "has_interactive_input", lambda: True)
    monkeypatch.setattr(sys, "argv", [
        "anywhere", "owner-passkey-init", "--state-dir", str(tmp_path),
    ])
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output["authenticator"] == "Choose Windows Hello on this computer"
    assert output["expires_in_seconds"] == 300
    parsed = urlsplit(output["registration_url"])
    assert parsed.scheme == "https" and parsed.hostname == "computer.example"
    assert parsed.path == "/owner-passkey"
    token = parse_qs(parsed.query)["ticket"][0]
    owner = _owner(tmp_path, vault)
    assert owner.is_passkey_only()
    assert OwnerPasskeys(owner, device=config.device).ticket_valid(token)
