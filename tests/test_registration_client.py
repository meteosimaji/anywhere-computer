import json
import sqlite3
from contextlib import closing

import pytest
from test_client_tokens import MemoryVault
from test_enrollment_credentials import saved

from anywhere_computer.client_tokens import ClientCredentialError
from anywhere_computer.enrollment_http import EnrollmentHTTPReply, EnrollmentTransportError
from anywhere_computer.private_directory import create_private_directory
from anywhere_computer.registration_client import RegistrationClient


@pytest.mark.parametrize("renewed_subject", ["original", "other"])
def test_fresh_grant_recovers_expired_pending_request_without_replacing_vault(
    tmp_path, renewed_subject,
):
    from test_enrollment_credentials import credentials as reopened_credentials

    from anywhere_computer.enrollment_credentials import EnrollmentToken

    vault = MemoryVault()
    original_credentials = saved(tmp_path / "credentials", vault)
    requests = []
    replies_lost = 2

    def wire(endpoint, fields, token):
        nonlocal replies_lost
        requests.append((endpoint, fields.copy(), token))
        if endpoint.endswith("/account"):
            subject = renewed_subject if token == "renewed-private-grant" else "original"
            return EnrollmentHTTPReply(200, {
                "issuer": original_credentials.issuer, "subject": subject,
            })
        if replies_lost:
            replies_lost -= 1
            raise EnrollmentTransportError(dispatched=True)
        return EnrollmentHTTPReply(200, {
            **fields, "device_id": "b" * 32, "state": "registered",
        })

    def opened(credentials):
        return RegistrationClient(tmp_path / "registration", credentials, wire=wire,
                                  endpoint="https://relay.example/register",
                                  account_endpoint="https://relay.example/account")

    first = opened(original_credentials)
    try:
        with pytest.raises(EnrollmentTransportError):
            first.register(attempt_id="a" * 32, name="日本語 PC 🚀")
        pending = first.current()
    finally:
        first.close()
    old_values = vault.data.copy()
    expired = reopened_credentials(tmp_path / "credentials", vault, now=1060)
    fresh = reopened_credentials(tmp_path / "renewal", vault, now=1060)
    fresh.save("c" * 32, EnrollmentToken(access_token="renewed-private-grant",
               token_type="Bearer", expires_in=60, scope="device:enroll"), requested_at=1060)
    all_values = vault.data.copy()
    client = opened(expired)
    try:
        with pytest.raises(ClientCredentialError):
            client.register(attempt_id="a" * 32, name=pending.name)
        if renewed_subject == "other":
            with pytest.raises(ValueError, match="different account"):
                client.recover(fresh, attempt_id="c" * 32)
        else:
            with pytest.raises(EnrollmentTransportError):
                client.recover(fresh, attempt_id="c" * 32)
        assert client.current() == pending
    finally:
        client.close()
    client = opened(expired)
    try:
        if renewed_subject == "original":
            result = client.recover(fresh, attempt_id="c" * 32)
            assert result.enrollment_id == pending.enrollment_id
            assert result.attempt_id == pending.attempt_id
            assert result.owner == pending.owner
            assert result.device.device_id == "b" * 32
            assert client.register(attempt_id="a" * 32, name=pending.name) == result
        registrations = [fields for endpoint, fields, _ in requests
                         if endpoint.endswith("/register")]
        assert len(registrations) == (3 if renewed_subject == "original" else 1)
        assert all(fields == registrations[0] for fields in registrations)
        assert vault.data == all_values and vault.writes == 2
        assert all(vault.data[key] == value for key, value in old_values.items())
    finally:
        client.close()
    for path in (tmp_path / "registration").iterdir():
        assert b"renewed-private-grant" not in path.read_bytes()


@pytest.mark.parametrize("invalid", [
    "missing_registration", "legacy", "endpoint", "issuer", "client", "attempt", "expired",
])
def test_recovery_rejects_unbound_or_invalid_grants_before_dispatch(tmp_path, invalid):
    from anywhere_computer.enrollment_credentials import EnrollmentCredentials, EnrollmentToken

    vault = MemoryVault()
    original = saved(tmp_path / "credentials", vault)
    calls = []

    def wire(endpoint, fields, token):
        calls.append(endpoint)
        if endpoint.endswith("/account"):
            return EnrollmentHTTPReply(200, {"issuer": original.issuer, "subject": "owner"})
        raise EnrollmentTransportError(dispatched=True)

    account_endpoint = None if invalid == "legacy" else "https://relay.example/account"
    client = RegistrationClient(tmp_path / "registration", original, wire=wire,
                                endpoint="https://relay.example/register",
                                account_endpoint=account_endpoint)
    try:
        if invalid != "missing_registration":
            with pytest.raises(EnrollmentTransportError):
                client.register(attempt_id="a" * 32, name="PC")
        pending = client.current()
    finally:
        client.close()
    now = [1000]
    fresh = EnrollmentCredentials(
        tmp_path / "renewal", vault=vault, clock=lambda: now[0], profile="test",
        issuer="https://other.example" if invalid == "issuer" else original.issuer,
        client="different" if invalid == "client" else original.client,
    )
    fresh.save("c" * 32, EnrollmentToken(access_token="renewed-private-grant",
               token_type="Bearer", expires_in=60, scope="device:enroll"), requested_at=1000)
    if invalid == "expired":
        now[0] = 1060
    client = RegistrationClient(
        tmp_path / "registration", original, wire=wire,
        endpoint="https://relay.example/register",
        account_endpoint="https://relay.example/changed" if invalid == "endpoint"
        else account_endpoint,
    )
    previous_calls, values = calls.copy(), vault.data.copy()
    try:
        with pytest.raises((ValueError, ClientCredentialError)):
            client.recover(fresh, attempt_id="d" * 32 if invalid == "attempt" else "c" * 32)
        assert calls == previous_calls
        assert client.current() == pending
        assert vault.data == values
    finally:
        client.close()


@pytest.mark.parametrize("account_endpoint", [
    "http://relay.example/account", "https://other.example/account",
    "https://relay.example:444/account", "https://relay.example/account?next=other",
])
def test_invalid_account_endpoint_does_not_create_registration_state(tmp_path, account_endpoint):
    credentials = saved(tmp_path / "credentials", MemoryVault())
    with pytest.raises(ValueError):
        RegistrationClient(tmp_path / "registration", credentials,
                           endpoint="https://relay.example/register",
                           account_endpoint=account_endpoint)
    assert not (tmp_path / "registration").exists()


def test_schema_one_pending_record_is_preserved_without_guessing_its_account(tmp_path):
    credentials = saved(tmp_path / "credentials", MemoryVault())
    directory = tmp_path / "registration"
    create_private_directory(directory)
    original = {"attempt_id": "a" * 32, "enrollment_id": "c" * 32,
                "name": "PC", "device": None}
    database = directory / "registration.sqlite3"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE registration (credential TEXT PRIMARY KEY, "
                   "endpoint TEXT NOT NULL, record TEXT NOT NULL)")
        db.execute("INSERT INTO registration VALUES(?,?,?)", (
            credentials.reference, "https://relay.example/register", json.dumps(original),
        ))
        db.execute("PRAGMA user_version=1")
    calls = []

    def wire(endpoint, fields, token):
        calls.append(endpoint)
        raise AssertionError("Unbound legacy registration must not be sent")

    client = RegistrationClient(directory, credentials, wire=wire,
                                endpoint="https://relay.example/register",
                                account_endpoint="https://relay.example/account")
    try:
        assert client.current().enrollment_id == original["enrollment_id"]
        assert client.current().owner is None
        with pytest.raises(ValueError):
            client.register(attempt_id="a" * 32, name="PC")
        assert calls == []
        assert client._db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert json.loads(client._db.execute("SELECT record FROM registration").fetchone()[0]) == (
            original
        )
    finally:
        client.close()


def test_account_binding_survives_reply_loss_and_rejects_other_account(tmp_path):
    credentials = saved(tmp_path / "credentials", MemoryVault())
    subject = "original"
    requests = []

    def wire(endpoint, fields, token):
        requests.append(endpoint)
        if endpoint.endswith("/account"):
            assert fields == {}
            return EnrollmentHTTPReply(200, {"issuer": credentials.issuer, "subject": subject})
        raise EnrollmentTransportError(dispatched=True)

    def opened(account_endpoint="https://relay.example/account"):
        return RegistrationClient(tmp_path / "registration", credentials,
                                  endpoint="https://relay.example/register", wire=wire,
                                  account_endpoint=account_endpoint)

    first = opened()
    try:
        with pytest.raises(EnrollmentTransportError):
            first.register(attempt_id="a" * 32, name="PC")
        pending = first.current()
        assert pending.owner.subject == "original"
    finally:
        first.close()
    subject = "other"
    for endpoint in ("https://relay.example/account", None):
        reopened = opened(endpoint)
        try:
            with pytest.raises(ValueError):
                reopened.register(attempt_id="a" * 32, name="PC")
            assert reopened.current() == pending
        finally:
            reopened.close()
    assert requests.count("https://relay.example/register") == 1
    subject = "original"
    reopened = opened()
    try:
        with pytest.raises(EnrollmentTransportError):
            reopened.register(attempt_id="a" * 32, name="PC")
        assert reopened.current() == pending
    finally:
        reopened.close()
    assert requests.count("https://relay.example/register") == 2


@pytest.mark.parametrize("confirmed", [False, True])
def test_expired_grant_preserves_original_registration_after_restart(tmp_path, confirmed):
    from test_enrollment_credentials import credentials as reopened_credentials

    vault = MemoryVault()
    credentials = saved(tmp_path / "credentials", vault)
    sent = []

    def wire(endpoint, fields, token):
        sent.append(fields.copy())
        if not confirmed:
            raise EnrollmentTransportError(dispatched=True)
        return EnrollmentHTTPReply(200, {**fields, "device_id": "b" * 32,
                                         "state": "registered"})

    first = RegistrationClient(tmp_path / "registration", credentials,
                               endpoint="https://relay.example/register", wire=wire)
    try:
        if confirmed:
            first.register(attempt_id="a" * 32, name="PC")
        else:
            with pytest.raises(EnrollmentTransportError):
                first.register(attempt_id="a" * 32, name="PC")
        original = first.current()
    finally:
        first.close()
    stored = vault.data.copy()
    expired = reopened_credentials(tmp_path / "credentials", vault, now=1060)
    reopened = RegistrationClient(tmp_path / "registration", expired,
                                  endpoint="https://relay.example/register", wire=wire)
    try:
        assert reopened.current() == original
        if confirmed:
            assert reopened.register(attempt_id="a" * 32, name="PC") == original
        else:
            with pytest.raises(ClientCredentialError):
                reopened.register(attempt_id="a" * 32, name="PC")
        assert reopened.current() == original
        assert len(sent) == 1
        assert vault.data == stored and vault.writes == 1
    finally:
        reopened.close()


def test_restart_after_response_loss_recovers_same_registration(tmp_path):
    credentials = saved(tmp_path / "credentials", MemoryVault())
    sent = []
    server_devices = {}

    def wire(endpoint, fields, token):
        assert endpoint == "https://relay.example/enrollment/devices"
        assert token == "synthetic-private-grant"
        sent.append(fields.copy())
        result = server_devices.setdefault(fields["enrollment_id"], {
            **fields, "device_id": "b" * 32, "state": "registered",
        })
        if len(sent) == 1:
            raise EnrollmentTransportError(dispatched=True)
        return EnrollmentHTTPReply(200, result)

    def opened():
        return RegistrationClient(tmp_path / "registration", credentials,
                                  endpoint="https://relay.example/enrollment/devices", wire=wire)

    first = opened()
    try:
        with pytest.raises(EnrollmentTransportError):
            first.register(attempt_id="a" * 32, name="日本語 PC 🚀")
        pending = first.current()
        assert pending is not None and pending.device is None
    finally:
        first.close()
    reopened = opened()
    try:
        result = reopened.register(attempt_id="a" * 32, name="日本語 PC 🚀")
        assert result.enrollment_id == pending.enrollment_id
        assert result.device.device_id == "b" * 32
        assert sent[0] == sent[1] and len(server_devices) == 1
        assert reopened.register(attempt_id="a" * 32, name="日本語 PC 🚀") == result
        assert len(sent) == 2
    finally:
        reopened.close()
    for path in (tmp_path / "registration").iterdir():
        assert b"synthetic-private-grant" not in path.read_bytes()


@pytest.mark.parametrize("change", ["name", "attempt", "endpoint"])
def test_pending_registration_cannot_change_identity(tmp_path, change):
    credentials = saved(tmp_path / "credentials", MemoryVault())
    calls = []

    def wire(endpoint, fields, token):
        calls.append(fields)
        raise EnrollmentTransportError(dispatched=True)

    original = RegistrationClient(tmp_path / "registration", credentials,
                                  endpoint="https://relay.example/register", wire=wire)
    try:
        with pytest.raises(EnrollmentTransportError):
            original.register(attempt_id="a" * 32, name="PC")
        pending = original.current()
    finally:
        original.close()
    second = RegistrationClient(tmp_path / "registration", credentials, wire=wire,
                                endpoint="https://other.example/register" if change == "endpoint"
                                else "https://relay.example/register")
    try:
        with pytest.raises(ValueError):
            second.register(attempt_id="c" * 32 if change == "attempt" else "a" * 32,
                            name="Other" if change == "name" else "PC")
        assert len(calls) == 1
        if change != "endpoint":
            assert second.current() == pending
    finally:
        second.close()


@pytest.mark.parametrize("kind", ["rejected", "mismatch", "malformed"])
def test_unconfirmed_reply_keeps_original_attempt(tmp_path, kind):
    credentials = saved(tmp_path / "credentials", MemoryVault())

    def wire(endpoint, fields, token):
        if kind == "rejected":
            return EnrollmentHTTPReply(401, {})
        if kind == "malformed":
            return EnrollmentHTTPReply(200, {"private": "synthetic-private-grant"})
        return EnrollmentHTTPReply(200, {**fields, "enrollment_id": "c" * 32,
                                        "device_id": "b" * 32, "state": "registered"})

    client = RegistrationClient(tmp_path / "registration", credentials,
                                endpoint="https://relay.example/register", wire=wire)
    try:
        with pytest.raises(ValueError) as caught:
            client.register(attempt_id="a" * 32, name="PC")
        assert "synthetic-private-grant" not in str(caught.value)
        assert client.current() is not None and client.current().device is None
    finally:
        client.close()


def test_mismatched_cached_receipt_is_not_returned_as_registered(tmp_path):
    credentials = saved(tmp_path / "credentials", MemoryVault())
    calls = []

    def wire(endpoint, fields, token):
        calls.append(fields)
        return EnrollmentHTTPReply(200, {**fields, "device_id": "b" * 32, "state": "registered"})

    client = RegistrationClient(tmp_path / "registration", credentials,
                                endpoint="https://relay.example/register", wire=wire)
    try:
        result = client.register(attempt_id="a" * 32, name="PC")
        corrupted = result.model_dump()
        corrupted["device"]["enrollment_id"] = "c" * 32
        with client._db:
            client._db.execute("UPDATE registration SET record=?", (json.dumps(corrupted),))
        with pytest.raises(ValueError):
            client.current()
        with pytest.raises(ValueError):
            client.register(attempt_id="a" * 32, name="PC")
        assert len(calls) == 1
    finally:
        client.close()
