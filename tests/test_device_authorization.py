import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from test_client_tokens import MemoryVault
from test_remote_transport import certificates as certificates

from anywhere_computer.client_tokens import ClientCredentialError, ClientTokens
from anywhere_computer.credentials import SERVICE
from anywhere_computer.device_authorization import DeviceAuthorizationClient, EnrollmentProvider
from anywhere_computer.enrollment_credentials import EnrollmentCredentials, EnrollmentToken
from anywhere_computer.enrollment_http import EnrollmentTransportError, https_enrollment_form


class Clock:
    value = 0.0

    def monotonic(self):
        return self.value

    def wall(self):
        return 1000.0 + self.value


@pytest.fixture
def endpoint(certificates):
    """Real local TLS, synthetic responses; not a deployed authorization service."""
    context, _ = certificates
    calls = []
    options = {"status": 200, "mode": "json", "pause": None}
    received, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            calls.append((self.path, fields))
            if options["pause"] == self.path:
                received.set()
                release.wait(timeout=3)
            if options["mode"] == "drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.send_response(options["status"])
            self.send_header("Content-Type", options.get("content_type", "application/json"))
            self.send_header("Location", "https://never-follow.invalid/device")
            self.end_headers()
            body = options.get("raw", json.dumps(options[self.path]).encode())
            try:
                if options["mode"] == "drip":
                    for part in body:
                        self.wfile.write(bytes([part]))
                        self.wfile.flush()
                        time.sleep(0.02)
                else:
                    self.wfile.write(body)
            except (OSError, ssl.SSLError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context("server", False).wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    issuer = f"https://localhost:{server.server_port}"
    provider = EnrollmentProvider(
        issuer=issuer, device_authorization_endpoint=issuer + "/device",
        token_endpoint=issuer + "/token", client_id="desktop", scope="device:enroll",
    )
    options["/device"] = {
        "device_code": "synthetic-device-secret", "user_code": "TEST-1234",
        "verification_uri": issuer + "/verify", "expires_in": 120,
        "verification_uri_complete": issuer + "/verify?user_code=TEST-1234",
    }
    options["/token"] = {
        "access_token": "synthetic-enrollment-token", "token_type": "Bearer",
        "expires_in": 900, "scope": "device:enroll",
    }
    tls = context("client", True)

    def wire(url, fields):
        return https_enrollment_form(url, fields, context=tls, timeout=2)

    try:
        yield provider, options, calls, wire, received, release, tls
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def client_for(tmp_path, endpoint, vault=None):
    provider, _, _, wire, *_ = endpoint
    clock = Clock()
    vault = vault or MemoryVault()
    store = EnrollmentCredentials(
        tmp_path, issuer=provider.issuer, client=provider.client_id,
        profile="new-pc", vault=vault, clock=clock.wall,
    )
    client = DeviceAuthorizationClient(
        provider, store, wire=wire, clock=clock.monotonic, wall_clock=clock.wall,
    )
    return client, store, vault, clock


def test_real_https_grant_saved_after_readback_without_publishing_secrets(tmp_path, endpoint):
    client, store, vault, clock = client_for(tmp_path, endpoint)
    provider, _, calls, *_ = endpoint
    initial = client.start()
    assert initial.phase == "waiting" and initial.user_code == "TEST-1234"
    assert client.poll().phase == "waiting" and len(calls) == 1
    clock.value = 5
    saved = client.poll()
    assert saved.phase == "grant_saved" and saved.credential_reference == store.reference
    assert saved.user_code is None and saved.verification_uri_complete is None
    record = json.loads(vault.get_password(SERVICE, store.reference))
    assert record["attempt_id"] == initial.attempt_id
    assert record["issuer"] == provider.issuer
    assert record["expires_at"] == 1905
    assert record["token"]["access_token"] == "synthetic-enrollment-token"
    assert calls == [
        ("/device", {"client_id": ["desktop"], "scope": ["device:enroll"]}),
        ("/token", {"client_id": ["desktop"], "device_code": ["synthetic-device-secret"],
                    "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"]}),
    ]
    for _ in range(2):
        assert client.start() == saved and client.poll() == saved and client.retry_save() == saved
    assert len(calls) == 2 and vault.writes == 1
    assert "synthetic" not in repr(saved) + saved.model_dump_json() + repr(initial)
    for path in tmp_path.iterdir():
        assert b"synthetic" not in path.read_bytes()


def test_real_tls_pending_slowdown_and_expiry_make_no_extra_requests(tmp_path, endpoint):
    client, _, vault, clock = client_for(tmp_path, endpoint)
    _, options, calls, *_ = endpoint
    options["/device"]["expires_in"] = 19
    client.start()
    options.update(status=400)
    options["/token"] = {"error": "slow_down"}
    clock.value = 5
    assert client.poll().retry_after == 10
    clock.value = 14
    assert client.poll().phase == "waiting" and len(calls) == 2
    clock.value = 15
    options["/token"] = {"error": "authorization_pending"}
    assert client.poll().retry_after == 4
    clock.value = 19
    assert client.poll().phase == "expired"
    assert len(calls) == 3 and not vault.data


@pytest.mark.parametrize("error,phase", [
    ("access_denied", "denied"), ("expired_token", "expired"),
    ("invalid_grant", "failed"), ("server_error", "failed"), ("synthetic-secret", "failed"),
])
def test_terminal_server_errors_stop_without_reusing_code(tmp_path, endpoint, error, phase):
    client, _, vault, clock = client_for(tmp_path, endpoint)
    _, options, calls, *_ = endpoint
    client.start()
    options.update(status=400)
    options["/token"] = {"error": error, "error_description": "synthetic-secret"}
    clock.value = 5
    result = client.poll()
    assert result.phase == phase
    assert "synthetic" not in result.model_dump_json()
    clock.value = 100
    assert client.poll().phase == phase and len(calls) == 2 and not vault.data


@pytest.mark.parametrize("mode", ["drop", "bad-json", "duplicate", "oversized", "redirect",
                                  "wrong-scope", "wrong-issuer", "bad-token", "html"])
def test_unconfirmed_exchange_is_never_replayed(tmp_path, endpoint, mode):
    client, _, vault, clock = client_for(tmp_path, endpoint)
    _, options, calls, *_ = endpoint
    client.start()
    if mode == "drop":
        options["mode"] = "drop"
    elif mode == "bad-json":
        options["raw"] = b"synthetic-not-json"
    elif mode == "duplicate":
        options["raw"] = b'{"error":"access_denied","error":"authorization_pending"}'
    elif mode == "oversized":
        options["raw"] = b"x" * 16385
    elif mode == "redirect":
        options["status"] = 302
    elif mode == "wrong-scope":
        options["/token"]["scope"] = "terminal_start"
    elif mode == "wrong-issuer":
        options["/token"]["iss"] = "https://other.invalid"
    elif mode == "bad-token":
        options["/token"]["access_token"] = "synthetic\r\ninvalid"
    elif mode == "html":
        options["content_type"] = "text/html"
    clock.value = 5
    assert client.poll().phase == "uncertain"
    clock.value = 100
    assert client.poll().phase == "uncertain"
    assert len(calls) == 2 and not vault.data


@pytest.mark.parametrize("path", ["/device", "/token"])
def test_cancel_in_flight_discards_late_result_and_concurrent_poll(tmp_path, endpoint, path):
    client, _, vault, clock = client_for(tmp_path, endpoint)
    _, options, calls, _, received, release, _ = endpoint
    if path == "/token":
        client.start()
        clock.value = 5
    options["pause"] = path
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.poll if path == "/token" else client.start)
        assert received.wait(timeout=2)
        assert client.poll().phase in {"starting", "requesting"}
        assert client.cancel().phase == "cancelled"
        release.set()
        assert future.result(timeout=3).phase == "cancelled"
    assert len(calls) == (1 if path == "/device" else 2) and not vault.data
    assert client.progress().user_code is None


@pytest.mark.parametrize("failure", ["write-before", "write-after", "readback"])
def test_failed_vault_publication_reconciles_without_network(tmp_path, endpoint, failure):
    class UnreliableVault(MemoryVault):
        failed = False

        def set_password(self, service, account, value):
            if failure == "write-before" and not self.failed:
                self.failed = True
                raise RuntimeError(value)
            super().set_password(service, account, value)
            if failure == "write-after" and not self.failed:
                self.failed = True
                raise RuntimeError(value)

        def get_password(self, service, account):
            result = super().get_password(service, account)
            if result is not None and failure == "readback" and not self.failed:
                self.failed = True
                raise RuntimeError(result)
            return result

    client, _, vault, clock = client_for(tmp_path, endpoint, UnreliableVault())
    client.start()
    clock.value = 5
    failed = client.poll()
    assert failed.phase == "credential_error" and failed.credential_reference is None
    assert "synthetic" not in failed.model_dump_json()
    assert client.poll() == failed
    assert client.retry_save().phase == "grant_saved"
    assert len(endpoint[2]) == 2
    assert len(vault.data) == 1


def test_other_enrollment_and_existing_ai_credentials_are_preserved(tmp_path, endpoint):
    client, store, vault, clock = client_for(tmp_path, endpoint)
    provider = endpoint[0]
    ai = ClientTokens(tmp_path, resource=provider.issuer + "/mcp", client="desktop",
                      profile="new-pc", vault=vault)
    vault.set_password(SERVICE, ai.account, "synthetic-existing-ai-credential")
    client.start()
    clock.value = 5
    assert client.poll().phase == "grant_saved"
    assert vault.get_password(SERVICE, ai.account) == "synthetic-existing-ai-credential"
    second = DeviceAuthorizationClient(provider, store, wire=endpoint[3])
    assert second.start().phase == "credential_error"
    assert len(endpoint[2]) == 2


def test_provider_mismatch_and_cross_origin_verification_rejected(tmp_path, endpoint):
    client, store, _, _ = client_for(tmp_path, endpoint)
    provider, options, calls, *_ = endpoint
    options["/device"]["verification_uri"] = "https://other.invalid/verify"
    assert client.start().phase == "failed" and len(calls) == 1
    with pytest.raises(ValueError):
        DeviceAuthorizationClient(provider.model_copy(update={"client_id": "other"}), store)
    with pytest.raises(ValueError):
        DeviceAuthorizationClient(provider.model_copy(update={
            "token_endpoint": "https://other.invalid/token",
        }), store)


def test_tls_hostname_ca_and_disabled_verification_rejected(endpoint):
    provider, _, calls, _, _, _, tls = endpoint
    fields = {"client_id": "desktop"}
    with pytest.raises(EnrollmentTransportError) as caught:
        https_enrollment_form(
            provider.device_authorization_endpoint.replace("localhost", "127.0.0.1"),
            fields, context=tls,
        )
    assert not caught.value.dispatched
    with pytest.raises(EnrollmentTransportError) as caught:
        https_enrollment_form(provider.device_authorization_endpoint, fields)
    assert not caught.value.dispatched and not calls
    insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verified TLS"):
        https_enrollment_form(provider.device_authorization_endpoint, fields, context=insecure)


def test_slow_drip_has_total_exchange_deadline(endpoint):
    provider, options, calls, _, _, _, tls = endpoint
    options["mode"] = "drip"
    started = time.monotonic()
    with pytest.raises(EnrollmentTransportError) as caught:
        https_enrollment_form(provider.device_authorization_endpoint, {}, context=tls, timeout=0.15)
    assert caught.value.dispatched and time.monotonic() - started < 2
    assert len(calls) == 1


def test_expired_grant_cannot_be_saved(tmp_path, endpoint):
    _, store, vault, clock = client_for(tmp_path, endpoint)
    token = EnrollmentToken(**endpoint[1]["/token"])
    clock.value = 1000
    with pytest.raises(ClientCredentialError):
        store.save("a" * 32, token, requested_at=1000)
    assert not vault.data


@pytest.mark.parametrize("changes", [
    {"iss": "https://other.invalid"}, {"expires_in": True},
    {"interval": 0}, {"device_code": "synthetic\ninvalid"},
    {"verification_uri_complete": "https://other.invalid/verify?user_code=TEST-1234"},
])
def test_bad_device_response_does_not_expose_code_or_poll(tmp_path, endpoint, changes):
    client, _, vault, clock = client_for(tmp_path, endpoint)
    endpoint[1]["/device"].update(changes)
    assert client.start().phase == "failed"
    clock.value = 5
    assert client.poll().phase == "failed"
    assert client.progress().user_code is None
    assert len(endpoint[2]) == 1 and not vault.data


def test_slow_initial_response_does_not_extend_code_lifetime(tmp_path, endpoint):
    _, store, _, clock = client_for(tmp_path, endpoint)
    provider, _, calls, wire, *_ = endpoint

    def slow_wire(url, fields):
        reply = wire(url, fields)
        clock.value = 121
        return reply

    client = DeviceAuthorizationClient(provider, store, wire=slow_wire, clock=clock.monotonic)
    assert client.start().phase == "expired"
    assert client.poll().phase == "expired" and len(calls) == 1


def test_only_pre_dispatch_connect_timeout_allows_retry(tmp_path, endpoint):
    _, store, vault, clock = client_for(tmp_path, endpoint)
    provider, _, calls, wire, *_ = endpoint
    failures = 0

    def unavailable_once(url, fields):
        nonlocal failures
        if url == provider.token_endpoint and failures == 0:
            failures += 1
            raise EnrollmentTransportError(dispatched=False, timeout=True)
        return wire(url, fields)

    client = DeviceAuthorizationClient(
        provider, store, wire=unavailable_once, clock=clock.monotonic, wall_clock=clock.wall,
    )
    client.start()
    clock.value = 5
    assert client.poll().retry_after == 10
    assert len(calls) == 1 and not vault.data
    clock.value = 14
    client.poll()
    assert len(calls) == 1
    clock.value = 15
    assert client.poll().phase == "grant_saved" and len(calls) == 2


def test_late_vault_write_does_not_replace_other_attempt(tmp_path, endpoint):
    _, store, vault, clock = client_for(tmp_path, endpoint)
    token = EnrollmentToken(**endpoint[1]["/token"])
    store.save("a" * 32, token, requested_at=clock.wall())
    original = vault.get_password(SERVICE, store.reference)
    with pytest.raises(ClientCredentialError, match="preserved"):
        store.save("b" * 32, token, requested_at=clock.wall())
    assert vault.get_password(SERVICE, store.reference) == original and vault.writes == 1


def test_credential_profiles_are_isolated(tmp_path, endpoint):
    _, store, vault, _ = client_for(tmp_path, endpoint)
    for changes in ({"issuer": "https://other.invalid"}, {"client": "other"},
                    {"profile": "other"}):
        other = EnrollmentCredentials(tmp_path, vault=vault, **{
            "issuer": store.issuer, "client": store.client, "profile": "new-pc", **changes,
        })
        assert other.reference != store.reference
