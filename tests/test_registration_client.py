import pytest
from test_client_tokens import MemoryVault
from test_enrollment_credentials import saved

from anywhere_computer.enrollment_http import EnrollmentHTTPReply, EnrollmentTransportError
from anywhere_computer.registration_client import RegistrationClient


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
