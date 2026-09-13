import io
import json

from test_client_tokens import MemoryVault
from test_device_authorization import Clock

from anywhere_computer.device_authorization import DeviceAuthorizationClient, EnrollmentProvider
from anywhere_computer.enrollment_credentials import EnrollmentCredentials
from anywhere_computer.enrollment_http import EnrollmentHTTPReply
from anywhere_computer.enrollment_worker import EnrollmentWorker, serve_enrollment
from anywhere_computer.registration_client import RegistrationClient


def worker_for(tmp_path):
    provider = EnrollmentProvider(issuer="https://auth.example",
        device_authorization_endpoint="https://auth.example/device",
        token_endpoint="https://auth.example/token", client_id="desktop", scope="device:enroll")
    clock, vault, calls = Clock(), MemoryVault(), []
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
        return EnrollmentHTTPReply(200, {**fields, "device_id": "b" * 32, "state": "registered"})

    auth = DeviceAuthorizationClient(provider, credentials, wire=auth_wire,
                                     clock=clock.monotonic, wall_clock=clock.wall)
    registration = RegistrationClient(tmp_path, credentials, endpoint="https://relay.example/enroll",
                                      wire=register_wire)
    return EnrollmentWorker(auth, registration), clock, calls


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
