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


def test_historical_grant_identity_requires_same_owner_device_and_client(authority):
    old = redeem(authority, approve(authority))
    current = redeem(authority, approve(authority))
    old_id = authority.verify(old.value, resource=RESOURCE).grant_id
    current_identity = authority.verify(current.value, resource=RESOURCE)
    assert authority.same_principal_grant(current_identity, old_id)
    authority.revoke(owner="owner", grant=old_id)
    assert authority.current_grant(old_id) is None
    assert authority.same_principal_grant(current_identity, old_id)
    authority.register_client("other-client", frozenset({REDIRECT}))
    other_client = redeem(authority, approve(authority, client="other-client"),
                          client="other-client")
    other_client_id = authority.verify(other_client.value, resource=RESOURCE).grant_id
    authority.enroll_device("owner", "other-device", TOOLS)
    other_device = redeem(authority, approve(authority, device="other-device"))
    other_device_id = authority.verify(other_device.value, resource=RESOURCE).grant_id
    assert not authority.same_principal_grant(current_identity, other_client_id)
    assert not authority.same_principal_grant(current_identity, other_device_id)
    assert not authority.same_principal_grant(current_identity, "unknown")


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
    # Preserve support for legacy finite grants.
    with authority.db:
        authority.db.execute("UPDATE grants SET expires=?", (time.time() + 86400,))
        authority.db.execute("UPDATE refresh_tokens SET expires=?", (time.time() + 86400,))
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
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert upgraded.verify(original.value, resource=RESOURCE) is not None
        assert upgraded.db.execute("SELECT count(*) FROM refresh_tokens").fetchone()[0] == 0
    finally:
        upgraded.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_reenable_never_restores_codes_access_or_refresh(authority, tmp_path, legacy):
    issued = redeem(authority, approve(authority))
    pending = approve(authority)
    if legacy:
        with authority.db:
            authority.db.execute("UPDATE authorized_devices SET active=0 WHERE id='device'")
    else:
        authority.revoke_device(owner="owner", device="device")
        assert (
            authority.db.execute("SELECT count(*) FROM grants WHERE revoked=0").fetchone()[0] == 0
        )
    assert not authority.device_enabled(owner="owner", device="device")
    assert authority.enable_device(owner="owner", device="device")
    assert authority.verify(issued.value, resource=RESOURCE) is None
    with pytest.raises(AuthorizationError):
        redeem(authority, pending)
    with pytest.raises(AuthorizationError):
        authority.refresh(refresh_token=issued.refresh_value, client="client", resource=RESOURCE)
    fresh = redeem(authority, approve(authority))
    assert not authority.enable_device(owner="owner", device="device")
    assert authority.verify(fresh.value, resource=RESOURCE) is not None
    reopened = AuthorizationStore(tmp_path, resource=RESOURCE, known_tools=TOOLS)
    try:
        assert reopened.device_enabled(owner="owner", device="device")
        assert reopened.verify(issued.value, resource=RESOURCE) is None
        assert reopened.verify(fresh.value, resource=RESOURCE) is not None
    finally:
        reopened.close()


def test_enable_rejects_unknown_owner_and_rolls_back_failed_reactivation(authority):
    issued = redeem(authority, approve(authority))
    for operation in (authority.enable_device, authority.revoke_device, authority.device_enabled):
        with pytest.raises(AuthorizationError):
            operation(owner="other", device="device")
        with pytest.raises(AuthorizationError):
            operation(owner="owner", device="missing")
    assert authority.verify(issued.value, resource=RESOURCE) is not None
    with authority.db:
        authority.db.execute("UPDATE authorized_devices SET active=0")
        authority.db.execute(
            "CREATE TRIGGER fail_enable BEFORE UPDATE OF active ON authorized_devices "
            "WHEN NEW.active=1 BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        authority.enable_device(owner="owner", device="device")
    assert not authority.device_enabled(owner="owner", device="device")
    assert authority.db.execute("SELECT revoked FROM grants").fetchone()[0] == 0
    assert authority.verify(issued.value, resource=RESOURCE) is None


def test_enabling_one_device_preserves_other_devices(authority):
    authority.enroll_device("owner", "other-device", TOOLS)
    other = redeem(authority, approve(authority, device="other-device"))
    authority.revoke_device(owner="owner", device="device")
    assert authority.enable_device(owner="owner", device="device")
    assert authority.verify(other.value, resource=RESOURCE) is not None


def test_v2_generation_upgrade_preserves_tokens_and_serializes_concurrent_open(authority, tmp_path):
    token = redeem(authority, approve(authority))
    with authority.db:
        authority.db.execute("ALTER TABLE authorized_devices DROP COLUMN generation")
        authority.db.execute("PRAGMA user_version=2")

    def reopen():
        store = AuthorizationStore(tmp_path, resource=RESOURCE, known_tools=TOOLS)
        try:
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
            assert store.db.execute("SELECT generation FROM authorized_devices").fetchone()[0] == 0
            return store.verify(token.value, resource=RESOURCE)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(executor.map(lambda _: reopen(), range(2)))


def test_permanent_grant_refreshes_years_later_but_access_expires(authority, monkeypatch):
    import time

    issued = redeem(authority, approve(authority))
    now = time.time()
    monkeypatch.setattr("anywhere_computer.authorization.time.time", lambda: now + 10 * 365 * 86400)
    assert authority.verify(issued.value, resource=RESOURCE) is None
    renewed = authority.refresh(
        refresh_token=issued.refresh_value, client="client", resource=RESOURCE
    )
    assert renewed.expires_in == 900
    identity = authority.verify(renewed.value, resource=RESOURCE)
    assert identity is not None
    authority.revoke(owner="owner", grant=identity.grant_id)
    with pytest.raises(AuthorizationError):
        authority.refresh(refresh_token=renewed.refresh_value, client="client", resource=RESOURCE)


def test_retain_active_grants_does_not_revive_expired_or_revoked(authority, monkeypatch):
    import time

    now = time.time()
    active = redeem(authority, approve(authority))
    expired = redeem(authority, approve(authority))
    revoked = redeem(authority, approve(authority))
    active_id = authority.verify(active.value, resource=RESOURCE).grant_id
    expired_id = authority.verify(expired.value, resource=RESOURCE).grant_id
    revoked_id = authority.verify(revoked.value, resource=RESOURCE).grant_id
    with authority.db:
        authority.db.execute("UPDATE grants SET expires=?", (now + 86400,))
        authority.db.execute("UPDATE refresh_tokens SET expires=?", (now + 86400,))
        authority.db.execute("UPDATE grants SET expires=? WHERE id=?", (now - 1, expired_id))
    authority.revoke(owner="owner", grant=revoked_id)
    with pytest.raises(AuthorizationError):
        authority.retain_active_grants(owner="other", device="device", client="client")
    assert authority.retain_active_grants(owner="owner", device="device", client="other") == 0
    assert authority.retain_active_grants(owner="owner", device="device", client="client") == 1
    assert authority.retain_active_grants(owner="owner", device="device", client="client") == 0
    assert authority.db.execute(
        "SELECT expires FROM grants WHERE id=?", (active_id,)
    ).fetchone() == (0,)
    monkeypatch.setattr("anywhere_computer.authorization.time.time", lambda: now + 86401)
    renewed = authority.refresh(
        refresh_token=active.refresh_value, client="client", resource=RESOURCE
    )
    assert authority.verify(renewed.value, resource=RESOURCE) is not None
    for token in (expired, revoked):
        with pytest.raises(AuthorizationError):
            authority.refresh(refresh_token=token.refresh_value, client="client", resource=RESOURCE)
