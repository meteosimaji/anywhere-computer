import json

import pytest
from test_client_tokens import MemoryVault

from anywhere_computer.client_tokens import ClientCredentialError
from anywhere_computer.credentials import SERVICE
from anywhere_computer.enrollment_credentials import EnrollmentCredentials, EnrollmentToken


def credentials(directory, vault, *, now=1000):
    return EnrollmentCredentials(directory, issuer="https://enrollment.example",
                                 client="desktop", profile="test", vault=vault,
                                 clock=lambda: now)


def saved(directory, vault):
    store = credentials(directory, vault)
    token = EnrollmentToken(access_token="synthetic-private-grant", token_type="Bearer",
                            expires_in=60, scope="device:enroll")
    store.save("a" * 32, token, requested_at=1000)
    return store


def test_reopen_grant_without_redeeming_code_or_writing_vault(tmp_path):
    vault = MemoryVault()
    first = saved(tmp_path, vault)
    reopened = credentials(tmp_path, vault, now=1059)
    assert reopened.reference == first.reference
    assert reopened.access_token(attempt_id="a" * 32, scope="device:enroll") == (
        "synthetic-private-grant"
    )
    assert vault.writes == 1
    for path in tmp_path.iterdir():
        assert b"synthetic-private-grant" not in path.read_bytes()


@pytest.mark.parametrize("now", [999, 1060, 1061, float("nan"), float("inf")])
def test_expiry_and_clock_rollback_preserve_record_without_releasing_token(tmp_path, now):
    vault = MemoryVault()
    store = saved(tmp_path, vault)
    original = vault.data.copy()
    with pytest.raises(ClientCredentialError):
        credentials(tmp_path, vault, now=now).access_token(
            attempt_id="a" * 32, scope="device:enroll",
        )
    assert vault.data == original and vault.writes == 1
    assert store.reference in [key[1] for key in vault.data]


@pytest.mark.parametrize("changes", [
    {"issuer": "https://other.example"}, {"client": "other"},
    {"attempt_id": "b" * 32}, {"version": 2}, {"expires_at": "1060"},
    {"unexpected": "synthetic-private-grant"},
])
def test_wrong_or_invalid_saved_binding_is_rejected_without_secret_errors(tmp_path, changes):
    vault = MemoryVault()
    store = saved(tmp_path, vault)
    key = SERVICE, store.reference
    record = json.loads(vault.data[key])
    vault.data[key] = json.dumps({**record, **changes})
    original = vault.data.copy()
    with pytest.raises(ClientCredentialError) as error:
        store.access_token(attempt_id="a" * 32, scope="device:enroll")
    assert "synthetic" not in str(error.value)
    assert vault.data == original and vault.writes == 1


@pytest.mark.parametrize("attempt,scope", [("b" * 32, "device:enroll"),
                                         ("a" * 32, "terminal_start")])
def test_caller_must_match_attempt_and_scope(tmp_path, attempt, scope):
    store = saved(tmp_path, MemoryVault())
    with pytest.raises(ClientCredentialError):
        store.access_token(attempt_id=attempt, scope=scope)


@pytest.mark.parametrize("raw", [None, "synthetic-corrupt", "x" * 16385])
def test_missing_or_corrupt_grants_are_not_replaced(tmp_path, raw):
    vault = MemoryVault()
    store = credentials(tmp_path, vault)
    if raw is not None:
        vault.data[SERVICE, store.reference] = raw
    with pytest.raises(ClientCredentialError):
        store.access_token(attempt_id="a" * 32, scope="device:enroll")
    assert vault.writes == 0


def test_saved_attempt_returns_identity_only_and_rejects_expiry(tmp_path):
    vault = MemoryVault()
    store = credentials(tmp_path, vault)
    assert store.saved_attempt(scope="device:enroll") is None
    saved(tmp_path, vault)
    assert store.saved_attempt(scope="device:enroll") == "a" * 32
    for scope in ("files_write", "device:enroll extra"):
        with pytest.raises(ClientCredentialError):
            store.saved_attempt(scope=scope)
    with pytest.raises(ClientCredentialError):
        credentials(tmp_path, vault, now=1060).saved_attempt(scope="device:enroll")
    assert vault.writes == 1


def test_forget_expired_profile_preserves_other_credentials(tmp_path):
    vault = MemoryVault()
    old = saved(tmp_path / "old", vault)
    target = saved(tmp_path / "target", vault)
    original = vault.data[SERVICE, old.reference]
    expired = credentials(tmp_path / "target", vault, now=2000)
    expired.forget(scope="device:enroll")
    expired.forget(scope="device:enroll")
    assert vault.data == {(SERVICE, old.reference): original}
    assert target.reference != old.reference


def test_cleanup_failure_keeps_retry_possible_without_disclosing_credentials(tmp_path):
    class FailedDelete(MemoryVault):
        fail = True

        def delete_password(self, service, account):
            if self.fail:
                return  # Backend claims success but leaves the item present.
            super().delete_password(service, account)

    vault = FailedDelete()
    store = saved(tmp_path, vault)
    with pytest.raises(ClientCredentialError) as caught:
        store.forget(scope="device:enroll")
    assert "synthetic" not in str(caught.value)
    assert vault.data
    vault.fail = False
    store.forget(scope="device:enroll")
    assert vault.data == {}


@pytest.mark.parametrize("changed", ["issuer", "client", "scope", "corrupt"])
def test_cleanup_refuses_mismatched_or_corrupt_record(tmp_path, changed):
    vault = MemoryVault()
    store = saved(tmp_path, vault)
    data = json.loads(vault.data[SERVICE, store.reference])
    if changed == "scope":
        data["token"]["scope"] = "files_read"
    elif changed != "corrupt":
        data[changed] = "unexpected"
    vault.data[SERVICE, store.reference] = "malformed" if changed == "corrupt" else json.dumps(data)
    original = vault.data.copy()
    with pytest.raises(ClientCredentialError):
        store.forget(scope="device:enroll")
    assert vault.data == original
