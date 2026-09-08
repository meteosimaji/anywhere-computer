import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from test_authorization import RESOURCE, TOOLS
from test_authorization import authority as authority
from test_client_tokens import MemoryVault
from test_remote_transport import certificates as certificates

from anywhere_computer import native_login
from anywhere_computer.authorization import AuthorizationError, pkce_s256
from anywhere_computer.client_tokens import (
    ClientAuthorizationRequired,
    ClientCredentialError,
    ClientTokens,
    TokenReply,
)
from anywhere_computer.native_login import _LoopbackCallback, https_code_exchange, login


@pytest.mark.parametrize("failure", [None, "lost", "scope", "save"])
async def test_native_browser_flow_saves_pair_and_closes_callback(authority, tmp_path, failure):
    authority.register_client("native", frozenset({"http://127.0.0.1/oauth/callback"}))
    vault = MemoryVault()
    tokens = ClientTokens(
        tmp_path / "client", resource=RESOURCE, client="native", profile="test", vault=vault
    )
    event_loop = asyncio.get_running_loop()
    observed = {}
    exchanges = []
    tokens.install(
        TokenReply(
            access_token="previous-access",
            refresh_token="previous-refresh",
            token_type="Bearer",
            expires_in=900,
            scope="files_read",
        ),
        requested_at=time.time(),
    )
    previous = dict(vault.data)
    if failure == "save":
        vault.fail_write = 2

    async def authorize(url):
        params = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        observed.update(params)
        code = authority.approve(
            owner="owner",
            device="device",
            client=params["client_id"],
            redirect=params["redirect_uri"],
            resource=params["resource"],
            tools=frozenset(params["scope"].split()),
            challenge=params["code_challenge"],
        )
        async with httpx.AsyncClient(trust_env=False) as http:
            response = await http.get(
                params["redirect_uri"], params={"code": code, "state": params["state"]}
            )
        assert response.status_code == 200
        assert code not in response.text
        return True

    def open_browser(url):
        return asyncio.run_coroutine_threadsafe(authorize(url), event_loop).result(5)

    async def redeem(resource, client, redirect, code, verifier):
        assert pkce_s256(verifier) == observed["code_challenge"]
        assert verifier not in repr(observed)
        issued = authority.exchange_code(
            code=code, verifier=verifier, client=client, redirect=redirect, resource=resource
        )
        return TokenReply(
            access_token=issued.value,
            refresh_token=issued.refresh_value,
            token_type="Bearer",
            expires_in=issued.expires_in,
            scope=issued.scope,
        )

    def exchange(*args):
        exchanges.append(True)
        result = asyncio.run_coroutine_threadsafe(redeem(*args), event_loop).result(5)
        if failure == "lost":
            raise ClientAuthorizationRequired("Unconfirmed exchange")
        if failure == "scope":
            return result.model_copy(update={"scope": "files_read files_write"})
        return result

    if failure:
        with pytest.raises((ClientAuthorizationRequired, ClientCredentialError)):
            await login(
                tokens, frozenset({"files_read"}), open_browser=open_browser, exchange=exchange
            )
        assert vault.data == previous
    else:
        await login(tokens, frozenset({"files_read"}), open_browser=open_browser, exchange=exchange)
        identity = authority.verify(tokens.access_token(), resource=RESOURCE)
        assert identity.tools == frozenset({"files_read"})
    assert exchanges == [True]
    port = urlsplit(observed["redirect_uri"]).port
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    for path in (tmp_path / "client").iterdir():
        assert tokens.access_token().encode() not in path.read_bytes()


@pytest.mark.parametrize(
    "failure", ["state", "host", "path", "duplicate", "issuer", "both", "origin"]
)
async def test_invalid_callback_cannot_consume_login(failure):
    callback = _LoopbackCallback("expected-state", "https://computer.example")
    url = await callback.start()
    params = {"state": "expected-state", "code": "synthetic-code"}
    headers = {}
    if failure == "state":
        params["state"] = "different"
    elif failure == "host":
        headers["Host"] = "evil.example"
    elif failure == "path":
        url += "/other"
    elif failure == "issuer":
        params["iss"] = "https://evil.example"
    elif failure == "both":
        params["error"] = "access_denied"
    elif failure == "origin":
        headers["Origin"] = "https://evil.example"
    query = urlencode(params) + ("&state=expected-state" if failure == "duplicate" else "")
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            response = await http.get(url + "?" + query, headers=headers)
            assert response.status_code == 400
            assert not callback.result.done()
            response = await http.get(
                f"http://{callback.authority}/oauth/callback",
                params={"state": "expected-state", "code": "valid-code"},
            )
            assert response.status_code == 200
            assert await callback.result == "valid-code"
    finally:
        await callback.close()


async def test_callback_denial_never_echoes_provider_description():
    callback = _LoopbackCallback("expected-state", "https://computer.example")
    url = await callback.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            response = await http.get(
                url,
                params={
                    "state": "expected-state",
                    "error": "access_denied",
                    "error_description": "secret text",
                },
            )
        assert "secret text" not in response.text
        with pytest.raises(ClientAuthorizationRequired, match="not approved"):
            await callback.result
    finally:
        await callback.close()


async def test_ipv6_fallback_uses_explicit_registered_host(authority, monkeypatch):
    authority.register_client(
        "native", frozenset({"http://127.0.0.1/oauth/callback", "http://[::1]/oauth/callback"})
    )
    original_start = asyncio.start_server

    async def ipv6_only(handler, host, port, **kwargs):
        if host == "127.0.0.1":
            raise OSError("Synthetic IPv4 unavailable")
        return await original_start(handler, host, port, **kwargs)

    monkeypatch.setattr(asyncio, "start_server", ipv6_only)
    callback = _LoopbackCallback("ipv6-state", "https://computer.example")
    try:
        try:
            redirect = await callback.start()
        except OSError:
            pytest.skip("This test host has no IPv6 loopback support")
        assert redirect.startswith("http://[::1]:")
        code = authority.approve(
            owner="owner",
            device="device",
            client="native",
            redirect=redirect,
            resource=RESOURCE,
            tools=frozenset({"files_read"}),
            challenge=pkce_s256("v" * 43),
        )
        async with httpx.AsyncClient(trust_env=False) as http:
            response = await http.get(redirect, params={"state": "ipv6-state", "code": code})
        assert response.status_code == 200
        assert await callback.result == code
    finally:
        await callback.close()


@pytest.mark.parametrize("opened", [False, True])
async def test_failed_or_timed_out_browser_does_not_exchange_or_replace_tokens(tmp_path, opened):
    vault = MemoryVault()
    tokens = ClientTokens(tmp_path, resource=RESOURCE, client="native", profile="test", vault=vault)
    calls = []
    with pytest.raises(ClientAuthorizationRequired):
        await login(
            tokens,
            frozenset({"files_read"}),
            open_browser=lambda url: opened,
            exchange=lambda *args: calls.append(args),
            timeout=0.05,
        )
    assert not calls and not vault.data


async def test_cancelled_login_closes_callback_without_saving(tmp_path):
    vault = MemoryVault()
    tokens = ClientTokens(tmp_path, resource=RESOURCE, client="native", profile="test", vault=vault)
    loop = asyncio.get_running_loop()
    launched = loop.create_future()

    def open_browser(url):
        loop.call_soon_threadsafe(launched.set_result, url)
        return True

    task = asyncio.create_task(login(tokens, frozenset({"files_read"}), open_browser=open_browser))
    url = await asyncio.wait_for(launched, 5)
    callback = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(OSError):
        await asyncio.open_connection(callback.hostname, callback.port)
    assert not vault.data


@pytest.mark.parametrize(
    "requested",
    [
        "http://127.0.0.1:54321/other",
        "http://127.0.0.1:54321/oauth/callback?new=1",
        "http://[::1]:54321/oauth/callback",
        "https://127.0.0.1:54321/oauth/callback",
        "http://127.0.0.1.evil.example:54321/oauth/callback",
    ],
)
def test_loopback_port_exception_never_changes_other_uri_parts(authority, requested):
    authority.register_client("native", frozenset({"http://127.0.0.1/oauth/callback"}))
    with pytest.raises((ValueError, AuthorizationError)):
        authority.approve(
            owner="owner",
            device="device",
            client="native",
            redirect=requested,
            resource=RESOURCE,
            tools=TOOLS,
            challenge=pkce_s256("v" * 43),
        )


def test_code_exchange_real_tls_bounded_and_never_retried(certificates, monkeypatch):
    context, _ = certificates
    pair = TokenReply(
        access_token="synthetic-access",
        refresh_token="synthetic-refresh",
        token_type="Bearer",
        expires_in=900,
        scope="files_read",
    )
    configured = {
        "status": 200,
        "type": "application/json",
        "body": pair.model_dump_json().encode(),
    }
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(
                (self.path, parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            )
            self.send_response(configured["status"])
            self.send_header("Content-Type", configured["type"])
            self.send_header("Location", "https://not-followed.example/")
            self.end_headers()
            self.wfile.write(configured["body"])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context("server", False).wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    trusted = context("client", True)
    monkeypatch.setattr(native_login.ssl, "create_default_context", lambda: trusted)
    resource = f"https://localhost:{server.server_port}/mcp"
    args = ("native", "http://127.0.0.1:45678/oauth/callback", "synthetic-code", "v" * 43)
    try:
        assert https_code_exchange(resource, *args) == pair
        assert calls[0][0] == "/oauth/token"
        assert calls[0][1]["code_verifier"] == ["v" * 43]
        assert calls[0][1]["redirect_uri"] == [args[1]]
        for change in (
            {"status": 302},
            {"body": b"synthetic-secret-error"},
            {"body": b"x" * 16385},
            {"type": "text/html"},
        ):
            configured.update(
                status=200, type="application/json", body=pair.model_dump_json().encode()
            )
            configured.update(change)
            before = len(calls)
            with pytest.raises(ClientAuthorizationRequired) as error:
                https_code_exchange(resource, *args)
            assert "synthetic" not in str(error.value)
            assert len(calls) == before + 1
        before = len(calls)
        with pytest.raises(ClientAuthorizationRequired):
            https_code_exchange(resource.replace("localhost", "127.0.0.1"), *args)
        assert len(calls) == before
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
