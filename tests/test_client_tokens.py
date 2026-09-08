import json
import multiprocessing
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from test_remote_transport import certificates as certificates

from anywhere_computer import client_tokens, credentials
from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.client_tokens import (
    ClientAuthorizationRequired,
    ClientCredentialError,
    ClientTokens,
    RefreshOutcomeUnknown,
    TokenReply,
    https_refresh,
)

RESOURCE = "https://computer.example/mcp"


class MemoryVault:
    """Synthetic credentials only; production never uses this memory backend."""

    def __init__(self, data=None):
        self.data = {} if data is None else data
        self.writes = 0
        self.fail_write = 0

    def get_password(self, service, account):
        return self.data.get((service, account))

    def set_password(self, service, account, value):
        self.writes += 1
        if self.writes == self.fail_write:
            raise RuntimeError("Synthetic secret must not escape: " + value)
        self.data[service, account] = value

    def delete_password(self, service, account):
        self.data.pop((service, account))


def pair(suffix="old", lifetime=900):
    return TokenReply(
        access_token="synthetic-access-" + suffix,
        refresh_token="synthetic-refresh-" + suffix,
        token_type="Bearer",
        expires_in=lifetime,
        scope="files_read",
    )


def manager(directory, vault, *, refresh=None, now=1000, **overrides):
    options = dict(resource=RESOURCE, client="client", profile="desktop", **overrides)
    if refresh is not None:
        options["refresh"] = refresh
    return ClientTokens(directory, vault=vault, clock=lambda: now, **options)


def test_saved_pair_reopens_and_profiles_are_isolated(tmp_path):
    vault = MemoryVault()
    first = manager(tmp_path, vault)
    first.install(pair(), requested_at=1000)
    reopened = manager(tmp_path, vault)
    assert reopened.access_token() == pair().access_token
    for resource, client, profile in (
        ("https://other.example/mcp", "client", "desktop"),
        (RESOURCE, "other-client", "desktop"),
        (RESOURCE, "client", "other-device"),
    ):
        other = ClientTokens(
            tmp_path, resource=resource, client=client, profile=profile, vault=vault
        )
        with pytest.raises(ClientAuthorizationRequired):
            other.access_token()
    for path in tmp_path.iterdir():
        assert b"synthetic-access" not in path.read_bytes()
        assert b"synthetic-refresh" not in path.read_bytes()
    assert "synthetic" not in repr(pair())
    reopened.forget()
    reopened.forget()
    with pytest.raises(ClientAuthorizationRequired):
        reopened.access_token()


def test_pending_intent_precedes_dispatch_and_new_pair_is_reused(tmp_path):
    vault = MemoryVault()
    initial = manager(tmp_path, vault)
    initial.install(pair(), requested_at=1000)
    calls = []

    def refresh(resource, client, token):
        stored = json.loads(vault.data[credentials.SERVICE, initial.account])
        assert stored["phase"] == "refresh_pending"
        assert (resource, client, token) == (RESOURCE, "client", pair().refresh_token)
        calls.append(token)
        return pair("new")

    renewal = manager(tmp_path, vault, now=1845, refresh=refresh)
    assert renewal.access_token() == pair("new").access_token
    assert manager(tmp_path, vault, now=1845).access_token() == pair("new").access_token
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["lost-response", "save-pair", "changed-scope", "same-token"])
def test_uncertain_rotation_never_resends_old_token(tmp_path, failure):
    vault = MemoryVault()
    original = manager(tmp_path, vault)
    original.install(pair(), requested_at=1000)
    calls = []
    if failure == "save-pair":
        vault.fail_write = 3  # Install, intent, then the replacement fails.

    def refresh(*args):
        calls.append(args)
        if failure == "lost-response":
            raise TimeoutError("synthetic-refresh-old")
        if failure == "changed-scope":
            return pair("new").model_copy(update={"scope": "files_read terminal_start"})
        if failure == "same-token":
            return pair()
        return pair("new")

    for _ in range(2):
        current = manager(tmp_path, vault, now=1845, refresh=refresh)
        with pytest.raises(RefreshOutcomeUnknown) as error:
            current.access_token()
        assert "synthetic" not in str(error.value)
    assert len(calls) == 1
    current.install(pair("reauthorized"), requested_at=1845)
    assert current.access_token() == pair("reauthorized").access_token


def test_intent_save_failure_never_dispatches_and_is_not_reauth(tmp_path):
    vault = MemoryVault()
    manager(tmp_path, vault).install(pair(), requested_at=1000)
    vault.fail_write = 2
    calls = []
    current = manager(tmp_path, vault, now=1845, refresh=lambda *args: calls.append(args))
    with pytest.raises(ClientCredentialError) as error:
        current.access_token()
    assert "synthetic" not in str(error.value)
    assert not calls


def _parallel_reader(directory, shared, barrier, output, *, crash=False):
    vault = MemoryVault(shared)

    def refresh(*args):
        shared["refresh-count"] = shared.get("refresh-count", 0) + 1
        if crash:
            os._exit(17)
        time.sleep(0.15)
        return pair("new")

    current = manager(directory, vault, now=1845, refresh=refresh)
    barrier.wait(timeout=10)
    output.put(current.access_token())


def test_processes_serialize_refresh_and_reread_keyring(tmp_path):
    context = multiprocessing.get_context("spawn")
    with context.Manager() as shared_manager:
        shared = shared_manager.dict()
        manager(tmp_path, MemoryVault(shared)).install(pair(), requested_at=1000)
        barrier, output = context.Barrier(2), context.Queue()
        children = [
            context.Process(target=_parallel_reader, args=(tmp_path, shared, barrier, output))
            for _ in range(2)
        ]
        try:
            for child in children:
                child.start()
            for child in children:
                child.join(timeout=15)
                assert child.exitcode == 0
            assert [output.get(timeout=3) for _ in children] == [pair("new").access_token] * 2
            assert shared["refresh-count"] == 1
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
            output.close()


def test_process_death_leaves_durable_intent_and_releases_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    with context.Manager() as shared_manager:
        shared = shared_manager.dict()
        vault = MemoryVault(shared)
        manager(tmp_path, vault).install(pair(), requested_at=1000)
        output = context.Queue()
        barrier = context.Barrier(1)
        child = context.Process(
            target=_parallel_reader,
            args=(tmp_path, shared, barrier, output),
            kwargs={"crash": True},
        )
        try:
            child.start()
            child.join(timeout=15)
            assert child.exitcode == 17
            with pytest.raises(RefreshOutcomeUnknown):
                manager(tmp_path, vault, now=1845).access_token()
            assert shared["refresh-count"] == 1
        finally:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
            output.close()


def test_client_rotation_preserves_real_server_grant(tmp_path):
    store = AuthorizationStore(
        tmp_path / "server", resource=RESOURCE, known_tools=frozenset({"files_read"})
    )
    callback = "https://client.example/callback"
    verifier = "v" * 43
    store.register_client("client", frozenset({callback}))
    store.enroll_device("owner", "device", frozenset({"files_read"}))
    code = store.approve(
        owner="owner",
        device="device",
        client="client",
        redirect=callback,
        resource=RESOURCE,
        tools=frozenset({"files_read"}),
        challenge=pkce_s256(verifier),
    )
    issued = store.exchange_code(
        code=code, verifier=verifier, client="client", redirect=callback, resource=RESOURCE
    )

    def as_reply(token):
        return TokenReply(
            access_token=token.value,
            refresh_token=token.refresh_value,
            expires_in=token.expires_in,
            scope=token.scope,
            token_type="Bearer",
        )

    def refresh(resource, client, token):
        return as_reply(store.refresh(resource=resource, client=client, refresh_token=token))

    try:
        current = ClientTokens(
            tmp_path / "client",
            resource=RESOURCE,
            client="client",
            profile="device",
            vault=MemoryVault(),
            refresh=refresh,
        )
        current.install(as_reply(issued), requested_at=time.time() - 845)
        renewed = current.access_token()
        assert renewed != issued.value
        assert store.verify(renewed, resource=RESOURCE) == store.verify(
            issued.value, resource=RESOURCE
        )
        assert current.access_token() == renewed
    finally:
        store.close()


def test_invalid_saved_and_received_credentials_hide_values(tmp_path):
    vault = MemoryVault()
    current = manager(tmp_path, vault)
    for changes in (
        {"expires_in": True},
        {"access_token": "synthetic\r\nsecret"},
        {"token_type": "Basic"},
        {"scope": " "},
        {"scope": "files_read  files_write"},
        {"scope": "files_read\\files_write"},
    ):
        with pytest.raises(ClientAuthorizationRequired) as error:
            current.install({**pair().model_dump(), **changes}, requested_at=1000)
        assert "synthetic" not in str(error.value)
    with pytest.raises(ClientAuthorizationRequired):
        current.install(pair().model_copy(update={"expires_in": True}), requested_at=1000)
    current.install(pair(), requested_at=1000)
    saved = json.loads(vault.data[credentials.SERVICE, current.account])
    del saved["phase"]
    vault.data[credentials.SERVICE, current.account] = json.dumps(saved)
    with pytest.raises(ClientAuthorizationRequired):
        current.access_token()
    vault.data[credentials.SERVICE, current.account] = "synthetic-corrupt-credential"
    with pytest.raises(ClientAuthorizationRequired) as error:
        current.access_token()
    assert "synthetic" not in str(error.value)


def test_native_backend_selector_still_rejects_plaintext(monkeypatch):
    class Plaintext:
        pass

    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: Plaintext())
    with pytest.raises(RuntimeError, match="OS credential store is required"):
        credentials.secure_backend()


def test_https_refresh_real_tls_and_bounded_responses(certificates, monkeypatch):
    context, _ = certificates
    configured = {"status": 200, "body": pair("new").model_dump_json().encode()}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(
                (self.path, parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            )
            self.send_response(configured["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Location", "https://other.example/never-follow")
            self.end_headers()
            self.wfile.write(configured["body"])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context("server", False).wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    trusted = context("client", True)
    monkeypatch.setattr(client_tokens.ssl, "create_default_context", lambda: trusted)
    resource = f"https://localhost:{server.server_port}/mcp"
    try:
        assert https_refresh(resource, "client", pair().refresh_token) == pair("new")
        assert requests[0] == (
            "/oauth/token",
            {
                "grant_type": ["refresh_token"],
                "client_id": ["client"],
                "resource": [resource],
                "refresh_token": [pair().refresh_token],
            },
        )
        for status, body in (
            (302, b"{}"),
            (200, b"x" * 16385),
            (200, b"not-json"),
            (503, b"synthetic"),
        ):
            configured.update(status=status, body=body)
            before = len(requests)
            with pytest.raises(RefreshOutcomeUnknown) as error:
                https_refresh(resource, "client", pair().refresh_token)
            assert len(requests) == before + 1  # No redirect or automatic retry.
            assert "synthetic" not in str(error.value)
        configured.update(status=400, body=b'{"error":"invalid_grant"}')
        with pytest.raises(ClientAuthorizationRequired, match="expired or was revoked"):
            https_refresh(resource, "client", pair().refresh_token)
        before = len(requests)
        with pytest.raises(RefreshOutcomeUnknown):
            https_refresh(
                resource.replace("localhost", "127.0.0.1"), "client", pair().refresh_token
            )
        assert len(requests) == before  # Hostname mismatch failed before sending credentials.
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_rejected_access_token_refreshes_once_and_stale_401_uses_saved_pair(tmp_path):
    vault = MemoryVault()
    calls = []

    def refresh(*args):
        calls.append(args)
        return pair("new")

    current = manager(tmp_path, vault, refresh=refresh)
    current.install(pair(), requested_at=1000)
    assert current.access_token(rejected_token=pair().access_token) == pair("new").access_token
    assert current.access_token(rejected_token=pair().access_token) == pair("new").access_token
    assert len(calls) == 1
