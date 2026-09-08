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


def test_refresh_rotates_preserves_identity_and_revokes_family_on_reuse(authority, tmp_path):
    first = redeem(authority, approve(authority))
    before = authority.verify(first.value, resource=RESOURCE)
    second = authority.refresh(
        refresh_token=first.refresh_value, client="client", resource=RESOURCE
    )
    assert second.value != first.value and second.refresh_value != first.refresh_value
    assert authority.verify(second.value, resource=RESOURCE) == before
    assert first.refresh_value not in repr(first) and second.refresh_value not in repr(second)
    for path in tmp_path.iterdir():
        if path.is_file():
            data = path.read_bytes()
            assert first.refresh_value.encode() not in data
            assert second.refresh_value.encode() not in data
    with pytest.raises(AuthorizationError, match="invalid_grant"):
        authority.refresh(refresh_token=first.refresh_value, client="client", resource=RESOURCE)
    assert authority.verify(first.value, resource=RESOURCE) is None
    assert authority.verify(second.value, resource=RESOURCE) is None
    with pytest.raises(AuthorizationError):
        authority.refresh(refresh_token=second.refresh_value, client="client", resource=RESOURCE)


def test_refresh_binding_and_lifetime(authority, monkeypatch):
    import time

    issued = redeem(authority, approve(authority))
    now = time.time()
    for override in (
        {"client": "other"},
        {"resource": "https://other.example/mcp"},
        {"scope": frozenset({"files_write"})},
    ):
        with pytest.raises(AuthorizationError):
            authority.refresh(
                **{
                    "refresh_token": issued.refresh_value,
                    "client": "client",
                    "resource": RESOURCE,
                    **override,
                }
            )
    monkeypatch.setattr("anywhere_computer.authorization.time.time", lambda: now + 901)
    assert authority.verify(issued.value, resource=RESOURCE) is None
    renewed = authority.refresh(
        refresh_token=issued.refresh_value, client="client", resource=RESOURCE
    )
    assert authority.verify(renewed.value, resource=RESOURCE) is not None
    monkeypatch.setattr("anywhere_computer.authorization.time.time", lambda: now + 86390)
    last = authority.refresh(
        refresh_token=renewed.refresh_value, client="client", resource=RESOURCE
    )
    assert 0 < last.expires_in <= 10
    monkeypatch.setattr("anywhere_computer.authorization.time.time", lambda: now + 86401)
    with pytest.raises(AuthorizationError):
        authority.refresh(refresh_token=last.refresh_value, client="client", resource=RESOURCE)


def test_parallel_refreshes_issue_once_and_detect_reuse(authority, tmp_path):
    original = redeem(authority, approve(authority))

    def attempt():
        store = AuthorizationStore(tmp_path, resource=RESOURCE, known_tools=TOOLS)
        try:
            try:
                return store.refresh(
                    refresh_token=original.refresh_value, client="client", resource=RESOURCE
                )
            except AuthorizationError:
                return None
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    issued = [token for token in results if token is not None]
    assert len(issued) == 1
    assert authority.verify(issued[0].value, resource=RESOURCE) is None


def test_version_one_upgrade_preserves_existing_access_tokens(authority, tmp_path):
    original = redeem(authority, approve(authority))
    with authority.db:
        # Version 1 had the same tables except refresh_tokens and did not issue refresh tokens.
        authority.db.execute("DROP TABLE refresh_tokens")
        authority.db.execute("PRAGMA user_version=1")
    upgraded = AuthorizationStore(tmp_path, resource=RESOURCE, known_tools=TOOLS)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert upgraded.verify(original.value, resource=RESOURCE) is not None
        assert upgraded.db.execute("SELECT count(*) FROM refresh_tokens").fetchone()[0] == 0
    finally:
        upgraded.close()
