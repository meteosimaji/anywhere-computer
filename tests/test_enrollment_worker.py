import io
import json

import pytest
from test_client_tokens import MemoryVault
from test_device_authorization import Clock

from anywhere_computer.device_authorization import DeviceAuthorizationClient, EnrollmentProvider
from anywhere_computer.enrollment_credentials import EnrollmentCredentials
from anywhere_computer.enrollment_http import EnrollmentHTTPReply, EnrollmentTransportError
from anywhere_computer.enrollment_worker import (
    EnrollmentWorker,
    NativeEnrollmentConfig,
    serve_enrollment,
)
from anywhere_computer.registration_client import RegistrationClient


def worker_for(tmp_path, *, vault=None, clock=None, account_binding=False,
               registration_losses=None):
    provider = EnrollmentProvider(issuer="https://auth.example",
        device_authorization_endpoint="https://auth.example/device",
        token_endpoint="https://auth.example/token", client_id="desktop", scope="device:enroll")
    clock = clock or Clock()
    vault = vault or MemoryVault()
    calls = []
    credentials = EnrollmentCredentials(tmp_path, issuer=provider.issuer, client=provider.client_id,
                                        profile="worker", vault=vault, clock=clock.wall)

    def auth_wire(endpoint, fields):
        calls.append(endpoint)
        if endpoint.endswith("/device"):
            return EnrollmentHTTPReply(200, {
                "device_code": "synthetic-private-code", "user_code": "TEST-CODE",
                "verification_uri": "https://auth.example/verify", "expires_in": 120,
            })
        return EnrollmentHTTPReply(200, {"access_token": "synthetic-private-token",
                                        "token_type": "Bearer", "scope": "device:enroll",
                                        "expires_in": 60})

    def register_wire(endpoint, fields, token):
        calls.append(endpoint)
        assert token == "synthetic-private-token"
        if endpoint.endswith("/account"):
            return EnrollmentHTTPReply(200, {"issuer": provider.issuer, "subject": "owner"})
        if registration_losses and registration_losses[0]:
            registration_losses[0] -= 1
            raise EnrollmentTransportError(dispatched=True)
        return EnrollmentHTTPReply(200, {**fields, "device_id": "b" * 32, "state": "registered"})

    auth = DeviceAuthorizationClient(provider, credentials, wire=auth_wire,
                                     clock=clock.monotonic, wall_clock=clock.wall)
    registration = RegistrationClient(tmp_path, credentials, endpoint="https://relay.example/enroll",
                                      wire=register_wire,
                                      account_endpoint="https://relay.example/account"
                                      if account_binding else None)

    def reauthorization(slot):
        recovery = EnrollmentCredentials(
            tmp_path, issuer=provider.issuer, client=provider.client_id,
            profile="recovery-" + slot, vault=vault, clock=clock.wall,
        )
        return DeviceAuthorizationClient(provider, recovery, wire=auth_wire,
                                          clock=clock.monotonic, wall_clock=clock.wall), recovery

    return EnrollmentWorker(auth, registration, reauthorization=reauthorization
                            if account_binding else None), clock, calls


def test_worker_reauthorizes_expired_pending_registration_and_reopens_new_grant(
    tmp_path, monkeypatch,
):
    from anywhere_computer.client_tokens import ClientCredentialError
    vault, clock, losses = MemoryVault(), Clock(), [1]

    def opened():
        return worker_for(tmp_path, vault=vault, clock=clock, account_binding=True,
                          registration_losses=losses)

    worker, _, _ = opened()
    worker.handle("start")
    clock.value = 6
    worker.handle("poll")
    with pytest.raises(EnrollmentTransportError):
        worker.handle("register", name="元の PC 🚀")
    original = worker.handle("progress")["registration"]
    old_vault = vault.data.copy()
    worker.close()
    clock.value = 80
    worker, _, calls = opened()
    expired = worker.handle("progress")
    assert expired["authorization"]["phase"] == "credential_error"
    assert expired["can_reauthorize"] is True and calls == []
    with pytest.raises(ValueError):
        worker.handle("cleanup")
    started = worker.handle("reauthorize")
    assert started["authorization"]["phase"] == "waiting"
    assert started["can_reauthorize"] is False
    with pytest.raises(ValueError):
        worker.handle("reauthorize")
    clock.value = 86
    granted = worker.handle("poll")
    assert granted["authorization"]["phase"] == "grant_saved"
    assert granted["registration"] == original
    worker.close()
    worker, _, calls = opened()
    try:
        restored = worker.handle("progress")
        assert restored["authorization"] == granted["authorization"]
        assert calls == []
        with pytest.raises(ValueError):
            worker.handle("register", name="Other PC")
        assert calls == []
        result = worker.handle("register", name="元の PC 🚀")
        assert result["registration"]["enrollment_id"] == original["enrollment_id"]
        assert result["registration"]["attempt_id"] == original["attempt_id"]
        assert result["registration"]["device"]["device_id"] == "b" * 32
        assert result["can_reauthorize"] is False
        assert result["can_cleanup"] is True
        assert calls == ["https://relay.example/account", "https://relay.example/enroll"]
        assert vault.writes == 2
        assert all(vault.data[key] == value for key, value in old_vault.items())
        public = json.dumps([expired, started, restored, result])
        assert "synthetic-private" not in public and '"owner"' not in public
        deletion = vault.delete_password
        monkeypatch.setattr(vault, "delete_password", lambda *args: None)
        with pytest.raises(ClientCredentialError):
            worker.handle("cleanup")
        assert worker.handle("progress")["can_cleanup"] is True
        assert worker.handle("progress")["registration"] == result["registration"]
        monkeypatch.setattr(vault, "delete_password", deletion)
        cleaned = worker.handle("cleanup")
        assert cleaned["can_cleanup"] is False
        assert cleaned["registration"] == result["registration"]
        assert vault.data == old_vault
        assert worker.handle("cleanup")["registration"] == result["registration"]
    finally:
        worker.close()


def test_worker_loss_before_new_grant_keeps_registration_and_records_next_slot(tmp_path):
    import sqlite3
    from contextlib import closing

    vault, clock = MemoryVault(), Clock()

    def opened():
        return worker_for(tmp_path, vault=vault, clock=clock, account_binding=True,
                          registration_losses=[1])[0]

    worker = opened()
    worker.handle("start")
    clock.value = 6
    worker.handle("poll")
    with pytest.raises(EnrollmentTransportError):
        worker.handle("register", name="PC")
    pending = worker.handle("progress")["registration"]
    worker.handle("reauthorize")
    worker.close()
    worker = opened()
    try:
        state = worker.handle("progress")
        assert state["registration"] == pending
        assert state["authorization"]["phase"] == "new"
        assert state["can_reauthorize"] is True
        state = worker.handle("reauthorize")
        assert state["authorization"]["phase"] == "waiting"
        assert state["registration"] == pending and vault.writes == 1
    finally:
        worker.close()
    with closing(sqlite3.connect(tmp_path / "registration.sqlite3")) as db:
        slots = db.execute("SELECT slot FROM reauthorizations ORDER BY rowid").fetchall()
        assert len(slots) == 2 and slots[0] != slots[1]
        assert db.execute("SELECT COUNT(*) FROM registration").fetchone()[0] == 1


def test_worker_preserves_authorization_through_registration(tmp_path):
    worker, clock, calls = worker_for(tmp_path)
    try:
        initial = worker.handle("start")
        clock.value = 6
        granted = worker.handle("poll")
        assert granted["authorization"]["attempt_id"] == initial["authorization"]["attempt_id"]
        assert granted["authorization"]["phase"] == "grant_saved"
        registered = worker.handle("register", name="日本語 PC 🚀")
        assert registered["registration"]["device"]["state"] == "registered"
        assert registered["connection_state"] == "not_checked"
        assert len(calls) == 3
        text = json.dumps([initial, granted, registered])
        assert "synthetic-private" not in text and "credential_reference" not in text
    finally:
        worker.close()


def test_line_commands_retain_state_and_refuse_arbitrary_actions(tmp_path):
    worker, _, calls = worker_for(tmp_path)
    source = io.StringIO('\n'.join(json.dumps(item) for item in [
        {"method": "start"}, {"method": "progress"},
        {"method": "terminal_start", "command": "unexpected"}, {"method": "cancel"},
    ]) + '\n')
    output = io.StringIO()
    serve_enrollment(worker, source, output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["result"] == responses[1]["result"]
    assert responses[2]["ok"] is False
    assert responses[3]["result"]["authorization"]["phase"] == "cancelled"
    assert calls == ["https://auth.example/device"]
    assert "synthetic-private" not in output.getvalue()


@pytest.mark.parametrize("invalid", ["scope", "endpoint", "unknown"])
def test_native_configuration_rejects_wrong_boundary(invalid):
    values = {
        "provider": {"issuer": "https://auth.example",
                     "device_authorization_endpoint": "https://auth.example/device",
                     "token_endpoint": "https://auth.example/token",
                     "client_id": "desktop", "scope": "device:enroll"},
        "registration_endpoint": "https://relay.example/enrollment",
    }
    if invalid == "scope":
        values["provider"]["scope"] = "files_write"
    elif invalid == "endpoint":
        values["registration_endpoint"] += "?token=unexpected"
    else:
        values["command"] = "unexpected"
    with pytest.raises(ValueError):
        NativeEnrollmentConfig.model_validate(values)


def test_explicit_restart_after_cancel_keeps_worker_and_changes_attempt(tmp_path):
    worker, clock, calls = worker_for(tmp_path)
    try:
        first = worker.handle("start")
        with pytest.raises(ValueError):
            worker.handle("restart")
        worker.handle("cancel")
        second = worker.handle("restart")
        assert second["authorization"]["phase"] == "waiting"
        assert first["authorization"]["attempt_id"] != second["authorization"]["attempt_id"]
        assert len(calls) == 2
        clock.value = 6
        worker.handle("poll")
        worker.handle("register", name="Restarted PC")
        with pytest.raises(ValueError):
            worker.handle("restart")
        assert len(calls) == 4
    finally:
        worker.close()


def test_worker_reopens_grant_saved_before_registration(tmp_path):
    vault, clock = MemoryVault(), Clock()
    worker, _, calls = worker_for(tmp_path, vault=vault, clock=clock)
    worker.handle("start")
    clock.value = 6
    original = worker.handle("poll")
    worker.close()
    assert len(calls) == 2
    reopened, _, new_calls = worker_for(tmp_path, vault=vault, clock=clock)
    try:
        restored = reopened.handle("progress")
        assert restored["authorization"]["phase"] == "grant_saved"
        assert restored["authorization"]["attempt_id"] == original["authorization"]["attempt_id"]
        assert new_calls == []
        result = reopened.handle("register", name="Recovered PC")
        assert result["registration"]["device"]["state"] == "registered"
        assert new_calls == ["https://relay.example/enroll"]
        assert vault.writes == 1
        assert "synthetic-private" not in json.dumps([restored, result])
    finally:
        reopened.close()
