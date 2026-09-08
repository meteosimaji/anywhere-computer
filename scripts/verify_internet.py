"""Explicit, temporary public-HTTPS probe; requires an existing cloudflared binary.

Only one disposable text file is exposed. No terminal, owner files, production
agent credentials, background service or permanent tunnel is used. A disposable
OAuth pair is saved in the OS keyring and removed during cleanup.
"""

import argparse
import asyncio
import dataclasses
import functools
import hashlib
import http.client
import ipaddress
import json
import re
import secrets
import shutil
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.client_tokens import ClientAuthorizationRequired, ClientTokens, TokenReply
from anywhere_computer.engine import Engine
from anywhere_computer.http_client import HTTPBackend, HTTPResponse
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.models import ReadFile, Request, WriteFile
from anywhere_computer.oauth_endpoints import OAuthEndpoints


class DNSNotReady(RuntimeError):
    pass


def resolve_public(host):
    try:
        addresses = sorted(
            {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        )
        resolver = "system"
    except socket.gaierror:
        query = urllib.parse.urlencode({"name": host, "type": "A"})
        req = urllib.request.Request(
            "https://cloudflare-dns.com/dns-query?" + query,
            headers={"Accept": "application/dns-json"},
        )
        with urllib.request.urlopen(
            req, timeout=15, context=ssl.create_default_context()
        ) as response:
            answer = json.loads(response.read(16384))
        addresses = sorted(
            {item["data"] for item in answer.get("Answer", []) if item.get("type") == 1}
        )
        resolver = "dns-over-https"
    if not addresses:
        raise DNSNotReady("Public DNS is not ready")
    if not all(ipaddress.ip_address(address).is_global for address in addresses):
        raise RuntimeError("Probe hostname did not resolve to public addresses")
    return addresses, resolver


class ResolvedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        self.probe_address = address
        self.probe_tls = ssl.create_default_context()
        super().__init__(host, timeout=15, context=self.probe_tls)

    def connect(self):
        raw = socket.create_connection((self.probe_address, self.port), self.timeout)
        try:
            # Keep normal CA and hostname verification even when supplying a DNS address.
            self.sock = self.probe_tls.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def request(url, *, address, method="GET", payload=None, headers=None, form=None):
    data = None
    headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Probe requires an HTTPS URL")
    connection = ResolvedHTTPSConnection(parsed.hostname, address)
    try:
        connection.request(method, parsed.path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise RuntimeError("Probe response exceeded limit")
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        return response.status, dict(response.headers.items()), body
    finally:
        connection.close()


def restricted_engine(directory):
    engine = Engine(directory / "agent")
    target = directory / "probe.txt"
    original = {
        name: engine.tools[name] for name in ("files_read", "files_write", "operations_get")
    }

    async def read(args):
        if not isinstance(args, ReadFile) or args.path != str(target) or target.is_symlink():
            raise ValueError("Probe only permits its disposable file")
        return await original["files_read"].handler(args)

    async def write(args):
        if (
            not isinstance(args, WriteFile)
            or args.path != str(target)
            or target.is_symlink()
            or len(args.text) > 1024
        ):
            raise ValueError("Probe only permits its disposable file")
        return await original["files_write"].handler(args)

    engine.tools = {
        "files_read": dataclasses.replace(original["files_read"], handler=read),
        "files_write": dataclasses.replace(original["files_write"], handler=write),
        "operations_get": original["operations_get"],
    }
    return engine, target


async def verify(receipt_path):
    executable = shutil.which("cloudflared")
    if executable is None:
        raise RuntimeError("Install an optional cloudflared test binary before running this probe")
    report = {
        "kind": "temporary-public-https",
        "started_at": time.time(),
        "completed": False,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Same Mac via public HTTPS edge; one disposable file; no browser login",
    }
    tunnel = None
    drain = None
    adapter = None
    engine = None
    store = None
    client_tokens = None
    remote_client = None
    with tempfile.TemporaryDirectory(prefix="anywhere-internet-") as raw:
        directory = Path(raw).resolve()
        backend = None
        try:
            engine, target = restricted_engine(directory)
            report["runtime_id"] = engine.runtime_id

            async def authenticate(token):
                return await backend.authenticate(token) if backend else None

            def session(owner):
                if backend is None:
                    raise RuntimeError("Probe has not initialized")
                return backend.session(owner)

            adapter = HTTPMCP(authenticate, session)
            port = await adapter.start()
            config = directory / "tunnel-config.yml"
            config.write_text("{}\n", encoding="utf-8")
            tunnel = await asyncio.create_subprocess_exec(
                executable,
                "tunnel",
                "--no-autoupdate",
                "--config",
                str(config),
                "--url",
                f"http://127.0.0.1:{port}",
                "--http-host-header",
                f"127.0.0.1:{port}",
                "--protocol",
                "http2",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            found = asyncio.get_running_loop().create_future()

            async def read_tunnel():
                assert tunnel.stdout is not None
                while line := await tunnel.stdout.readline():
                    if b"Registered tunnel connection" in line:
                        report["tunnel_connected"] = True
                    matched = re.search(rb"https://[a-z0-9-]+\.trycloudflare\.com", line)
                    if matched and not found.done():
                        found.set_result(matched.group().decode("ascii"))
                if not found.done():
                    found.set_exception(
                        RuntimeError("Temporary tunnel exited before publishing URL")
                    )

            drain = asyncio.create_task(read_tunnel())
            public = await asyncio.wait_for(found, 60)
            report["public_url"] = public
            print(json.dumps({"stage": "tunnel_created", "public_url": public}), flush=True)
            store = AuthorizationStore(
                directory / "authorization",
                resource=public + "/mcp",
                known_tools=frozenset(engine.tools),
            )
            callback = "https://example.com/disposable-probe-callback"
            store.register_client("probe-client", frozenset({callback}))
            store.enroll_device("probe-owner", "probe-device", frozenset(engine.tools))
            backend = AuthorizedDeviceMCP(store, engine, owner="probe-owner", device="probe-device")
            oauth = OAuthEndpoints(store, authorization_endpoint=public + "/not-implemented")
            adapter.public_routes = oauth.routes()
            adapter.auth_challenge = oauth.challenge
            dns_deadline = time.monotonic() + 120
            while True:
                try:
                    addresses, resolver = await asyncio.to_thread(
                        resolve_public, urllib.parse.urlsplit(public).hostname
                    )
                    break
                except (DNSNotReady, OSError, urllib.error.URLError):
                    if time.monotonic() >= dns_deadline:
                        raise RuntimeError("Public DNS readiness timed out") from None
                    await asyncio.sleep(2)

            report["public_addresses"] = addresses
            report["dns_resolver"] = resolver
            public_request = functools.partial(request, address=addresses[0])
            deadline = time.monotonic() + 60
            while True:
                try:
                    status, _, body = await asyncio.to_thread(
                        public_request, public + "/.well-known/oauth-protected-resource"
                    )
                    report["last_metadata_status"] = status
                    if status == 200 and body and body.get("resource") == public + "/mcp":
                        break
                except (OSError, urllib.error.URLError) as error:
                    report["last_network_error"] = type(error).__name__
                if time.monotonic() >= deadline:
                    raise RuntimeError("Public metadata did not become reachable")
                await asyncio.sleep(2)  # GET readiness only; mutations are never retried.
            report["metadata_verified"] = True
            print(json.dumps({"stage": "public_metadata_verified"}), flush=True)
            verifier = secrets.token_urlsafe(32)
            code = store.approve(
                owner="probe-owner",
                device="probe-device",
                client="probe-client",
                redirect=callback,
                resource=public + "/mcp",
                tools=frozenset(engine.tools),
                challenge=pkce_s256(verifier),
            )
            code_requested_at = time.time()
            status, _, token_body = await asyncio.to_thread(
                public_request,
                public + "/oauth/token",
                method="POST",
                form={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "client_id": "probe-client",
                    "redirect_uri": callback,
                    "resource": public + "/mcp",
                },
            )
            if status != 200 or not token_body or "access_token" not in token_body:
                raise RuntimeError("Public token exchange failed")

            def refresh_client(resource, client, refresh_token):
                status, _, renewed = public_request(
                    public + "/oauth/token",
                    method="POST",
                    form={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": client,
                        "resource": resource,
                    },
                )
                if status != 200:
                    raise ClientAuthorizationRequired("Public client authorization was rejected")
                return TokenReply.model_validate(renewed)

            client_tokens = ClientTokens(
                directory / "client",
                resource=public + "/mcp",
                client="probe-client",
                profile="disposable-probe",
                refresh=refresh_client,
            )
            await asyncio.to_thread(
                client_tokens.install, token_body, requested_at=code_requested_at
            )
            dropped_write = False
            write_posts = 0
            rejected_after_revocation = False

            def public_wire(resource, method, packet, headers):
                nonlocal dropped_write, write_posts, rejected_after_revocation
                status, response_headers, body = public_request(
                    resource, method=method, payload=packet, headers=headers
                )
                if status == 401:
                    rejected_after_revocation = True
                if (
                    packet
                    and packet.get("method") == "tools/call"
                    and packet.get("params", {}).get("name") == "files_write"
                ):
                    write_posts += 1
                    if not dropped_write and status == 200:
                        dropped_write = True
                        # The server's response arrived over HTTPS. Drop it here to
                        # exercise the same client's unknown-result recovery path.
                        raise ConnectionError("Injected loss of a completed write response")
                return HTTPResponse(
                    status, {key.lower(): value for key, value in response_headers.items()}, body
                )

            remote_client = HTTPBackend(client_tokens, wire=public_wire)
            catalog = await remote_client.catalog()
            if {tool["name"] for tool in catalog} != set(engine.tools):
                raise RuntimeError("Unexpected public tool exposure")
            text = "Anywhere Computer インターネット接続試験 " + secrets.token_hex(8)
            write = Request(
                operation_id=secrets.token_hex(16),
                tool="files_write",
                arguments={"path": str(target), "text": text},
            )
            unknown = await remote_client.execute(write)
            if unknown.state != "unknown" or write_posts != 1:
                raise RuntimeError("Client did not preserve an unknown write without replay")
            recovered = await remote_client.execute(
                Request(
                    operation_id=secrets.token_hex(16),
                    tool="operations_get",
                    arguments={"operation_id": write.operation_id},
                )
            )
            if recovered.state != "completed" or recovered.data.get("state") != "completed":
                raise RuntimeError("Client could not recover the lost public write result")
            report["lost_write_response_recovered"] = True
            report["write_dispatch_count"] = write_posts

            # Age the client deadline to test renewal without claiming a 15-minute soak.
            await asyncio.to_thread(
                client_tokens.install, token_body, requested_at=time.time() - 845
            )
            report["client_expiry_age_simulated_seconds"] = 845
            previous_session = remote_client.session_id
            read = await remote_client.execute(
                Request(
                    operation_id=secrets.token_hex(16),
                    tool="files_read",
                    arguments={"path": str(target)},
                )
            )
            renewed_access = await asyncio.to_thread(client_tokens.access_token)
            if (
                read.state != "completed"
                or read.data.get("text") != text
                or renewed_access == token_body["access_token"]
                or remote_client.session_id != previous_session
            ):
                raise RuntimeError("Public session did not survive automatic token renewal")
            reopened_tokens = ClientTokens(
                directory / "client",
                resource=public + "/mcp",
                client="probe-client",
                profile="disposable-probe",
                refresh=refresh_client,
            )
            if await asyncio.to_thread(reopened_tokens.access_token) != renewed_access:
                raise RuntimeError("OS credential store did not preserve the renewed pair")
            report["client_keyring_reopen_verified"] = True
            report["refresh_rotation_verified"] = True

            # Explicit server-side session expiry: client observes 404 and reinitializes.
            adapter.sessions.clear()
            read = await remote_client.execute(
                Request(
                    operation_id=secrets.token_hex(16),
                    tool="files_read",
                    arguments={"path": str(target)},
                )
            )
            if (
                read.state != "completed"
                or read.data.get("text") != text
                or remote_client.session_id == previous_session
            ):
                raise RuntimeError("Client did not recover an expired public MCP session")
            report["session_recovery_verified"] = True
            report["write_read_across_sessions"] = True
            store.revoke_device(owner="probe-owner", device="probe-device")
            denied = await remote_client.execute(
                Request(
                    operation_id=secrets.token_hex(16),
                    tool="files_read",
                    arguments={"path": str(target)},
                )
            )
            if denied.state != "failed" or not rejected_after_revocation:
                raise RuntimeError("Public request remained authorized after device revocation")
            report["revocation_verified"] = True
            report["completed"] = True
        except Exception as error:
            report["failure_type"] = type(error).__name__
            # Do not persist exception text, requests, tokens, codes, or tunnel logs.
            raise
        finally:
            if remote_client:
                await remote_client.close()
            if tunnel and tunnel.returncode is None:
                tunnel.terminate()
                try:
                    await asyncio.wait_for(tunnel.wait(), 10)
                except TimeoutError:
                    tunnel.kill()
                    await tunnel.wait()
            if drain:
                await asyncio.gather(drain, return_exceptions=True)
            if client_tokens:
                try:
                    await asyncio.to_thread(client_tokens.forget)
                    report["client_keyring_removed"] = True
                except Exception:
                    report["client_keyring_removed"] = False
                    report["completed"] = False
                    report["failure_type"] = "ClientCredentialCleanupFailed"
            if adapter:
                await adapter.close()
            if store:
                store.close()
            if engine:
                await engine.close()
            report["tunnel_stopped"] = tunnel is None or tunnel.returncode is not None
            report["finished_at"] = time.time()
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["temporary_files_removed"] = not directory.exists()
    receipt_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    if not report["completed"]:
        raise RuntimeError("Internet probe did not complete cleanly")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=Path("dist/internet-verification.json"))
    arguments = parser.parse_args()
    try:
        asyncio.run(verify(arguments.receipt))
    except Exception as error:
        print(json.dumps({"completed": False, "failure_type": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
