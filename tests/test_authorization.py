from concurrent.futures import ThreadPoolExecutor

import pytest

from anywhere_computer.authorization import AuthorizationError, AuthorizationStore, pkce_s256

RESOURCE = "https://computer.example/mcp"
REDIRECT = "https://client.example/callback"
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
TOOLS = frozenset({"computer_status", "files_read", "files_write", "operations_get"})


@pytest.fixture
def authority(tmp_path):
    store = AuthorizationStore(tmp_path, resource=RESOURCE, known_tools=TOOLS)
    store.register_client("client", frozenset({REDIRECT}))
    store.enroll_device("owner", "device", TOOLS)
    try:
        yield store
    finally:
        store.close()


def approve(store, **overrides):
    return store.approve(
        **{
            "owner": "owner",
            "device": "device",
            "client": "client",
            "redirect": REDIRECT,
            "resource": RESOURCE,
            "tools": frozenset({"files_read"}),
            "challenge": pkce_s256(VERIFIER),
            **overrides,
        }
    )


def redeem(store, code, **overrides):
    return store.exchange_code(
        **{
            "code": code,
            "verifier": VERIFIER,
            "client": "client",
            "redirect": REDIRECT,
            "resource": RESOURCE,
            **overrides,
        }
    )


def test_pkce_rfc7636_vector():
    assert pkce_s256(VERIFIER) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    with pytest.raises(AuthorizationError):
        pkce_s256("too-short")


def test_token_identity_persists_without_raw_secrets(authority, tmp_path):
    code = approve(authority)
    token = redeem(authority, code)
    identity = authority.verify(token.value, resource=RESOURCE)
    assert identity.owner == "owner" and identity.device == "device"
    assert identity.tools == frozenset({"files_read"})
    assert token.value not in repr(token)
    other = AuthorizationStore(tmp_path, resource=RESOURCE, known_tools=TOOLS)
    try:
        assert other.verify(token.value, resource=RESOURCE) == identity
    finally:
        other.close()
    for path in tmp_path.iterdir():
        if path.is_file():
            data = path.read_bytes()
            assert code.encode() not in data and token.value.encode() not in data
            assert VERIFIER.encode() not in data


@pytest.mark.parametrize(
    "override",
    [
        {"client": "other"},
        {"redirect": REDIRECT + "/extra"},
        {"resource": "https://other.example/mcp"},
        {"verifier": "x" * 43},
    ],
)
def test_mismatched_exchange_never_issues_or_consumes_code(authority, override):
    code = approve(authority)
    with pytest.raises(AuthorizationError):
        redeem(authority, code, **override)
    token = redeem(authority, code)
    assert authority.verify(token.value, resource=RESOURCE) is not None
    assert authority.verify(token.value, resource="https://other.example/mcp") is None


def test_code_replay_revokes_original_token(authority):
    code = approve(authority)
    token = redeem(authority, code)
    with pytest.raises(AuthorizationError, match="invalid_grant"):
        redeem(authority, code)
    assert authority.verify(token.value, resource=RESOURCE) is None


def test_parallel_redemptions_issue_only_once(authority, tmp_path):
    code = approve(authority)

    def attempt():
        store = AuthorizationStore(tmp_path, resource=RESOURCE, known_tools=TOOLS)
        try:
            try:
                return redeem(store, code)
            except AuthorizationError:
                return None
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    issued = [token for token in results if token is not None]
    assert len(issued) == 1
    assert authority.verify(issued[0].value, resource=RESOURCE) is None


def test_owner_permissions_expiry_and_device_revoke(authority, monkeypatch):
    import time

    with pytest.raises(AuthorizationError):
        approve(authority, owner="other")
    with pytest.raises(ValueError):
        approve(authority, tools=frozenset({"operations_recent"}))
    with pytest.raises(AuthorizationError):
        approve(authority, redirect=REDIRECT + "?unregistered")
    code = approve(authority)
    now = time.time()
    monkeypatch.setattr("anywhere_computer.authorization.time.time", lambda: now + 121)
    with pytest.raises(AuthorizationError):
        redeem(authority, code)
    monkeypatch.setattr("anywhere_computer.authorization.time.time", lambda: now)
    token = redeem(authority, approve(authority))
    grant = authority.verify(token.value, resource=RESOURCE)
    with pytest.raises(AuthorizationError):
        authority.revoke(owner="other", grant=grant.grant_id)
    assert authority.verify(token.value, resource=RESOURCE) is not None
    monkeypatch.setattr("anywhere_computer.authorization.time.time", lambda: now + 901)
    assert authority.verify(token.value, resource=RESOURCE) is None
    monkeypatch.setattr("anywhere_computer.authorization.time.time", lambda: now)
    authority.revoke_device(owner="owner", device="device")
    assert authority.verify(token.value, resource=RESOURCE) is None
    with pytest.raises(AuthorizationError):
        approve(authority)


def test_store_cannot_change_resource(authority, tmp_path):
    with pytest.raises(ValueError, match="different resource"):
        AuthorizationStore(tmp_path, resource="https://other.example/mcp", known_tools=TOOLS)


@pytest.mark.parametrize(
    "redirect",
    [
        "http://client.example/callback",
        "https://client.example/callback#fragment",
        "https://user:secret@client.example/callback",
        "https://client.example/\r\nheader",
    ],
)
def test_registration_rejects_unsafe_callback_urls(authority, redirect):
    with pytest.raises(ValueError):
        authority.register_client("bad", frozenset({redirect}))
