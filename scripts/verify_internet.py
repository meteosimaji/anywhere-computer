"""Explicit public-HTTPS probe; requires an existing cloudflared binary.

Only a disposable text file and generated binary download are exposed. No terminal,
owner files or production agent credentials are exposed. By default a temporary
tunnel is used. --tunnel-state-dir selects an existing dedicated constant tunnel
and tests an owned connector crash; its saved token and provider route are retained.
Disposable OAuth credentials, files and processes are removed during cleanup.
"""

import argparse
import asyncio
import base64
import dataclasses
import functools
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import secrets
import shutil
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psutil

from anywhere_computer.authorization import AuthorizationStore
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.browser_authorization import BrowserAuthorization
from anywhere_computer.client_tokens import ClientAuthorizationRequired, ClientTokens, TokenReply
from anywhere_computer.cloudflare_tunnel import TunnelCredential
from anywhere_computer.downloads import DOWNLOAD_TOOLS
from anywhere_computer.engine import Engine
from anywhere_computer.files import read_bytes
from anywhere_computer.http_client import HTTPBackend, HTTPResponse
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.http_service import load_http_config
from anywhere_computer.models import BeginDownload, ReadFile, Request, WriteFile
from anywhere_computer.native_login import login
from anywhere_computer.oauth_endpoints import OAuthEndpoints
from anywhere_computer.owner_credentials import OwnerCredentials


class DNSNotReady(RuntimeError):
    pass


def require_no_store(headers):
    values = [value for name, value in headers.items() if name.lower() == "cache-control"]
    if not values or "no-store" not in {part.strip().lower() for part in values[0].split(",")}:
        raise RuntimeError("Public authentication/MCP response lost its no-store header")


def load_probe_tunnel(directory):
    """Refuse ordinary service profiles before opening their credential store.

    This owner-controlled marker prevents accidental profile selection, not a
    malicious local owner. Provisioning must verify a newly dedicated provider
    tunnel/route before recording this binding; a normal token is not sufficient.
    """
    config = load_http_config(directory)
    marker = directory / "provisioning.json"
    if not marker.is_file() or marker.is_symlink():
        raise ValueError("Constant probe requires its dedicated provisioning marker")
    data = read_bytes(marker)
    if len(data) > 16384:
        raise ValueError("Probe provisioning marker exceeds limit")
    record = json.loads(data)
    if (
        not isinstance(record, dict)
        or record.get("purpose") != "Anywhere Computer isolated constant HTTPS development probe"
        or record.get("phase") != "route_ready_for_probe"
        or record.get("hostname") != urllib.parse.urlsplit(config.resource).hostname
        or record.get("port") != config.port
        or not isinstance(record.get("probe_binding"), dict)
    ):
        raise ValueError("State directory is not an enrolled constant probe")
    binding = record["probe_binding"]
    expected = {
        "state_directory": str(directory.resolve()),
        "resource": config.resource,
        "device": config.device,
        "port": config.port,
    }
    if any(binding.get(name) != value for name, value in expected.items()):
        raise ValueError("Probe configuration differs from its dedicated binding")
    credential = TunnelCredential(directory)
    digest = hashlib.sha256(credential.read().encode("ascii")).hexdigest()
    fingerprint = binding.get("token_sha256")
    if (
        binding.get("credential_account") != credential.account
        or not isinstance(fingerprint, str)
        or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
        or not hmac.compare_digest(fingerprint, digest)
    ):
        raise ValueError("Tunnel credential differs from the dedicated probe enrollment")
    return config


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


def request(url, *, address, method="GET", payload=None, headers=None, form=None, html=False):
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
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection.request(method, target, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise RuntimeError("Probe response exceeded limit")
        try:
            body = raw.decode("utf-8") if html else (json.loads(raw) if raw else None)
        except ValueError:
            body = None
        return response.status, dict(response.headers.items()), body
    finally:
        connection.close()


def restricted_engine(directory):
    engine = Engine(directory / "agent")
    target = directory / "probe.txt"
    binary_source = directory / "download.bin"
    with binary_source.open("wb") as output:
        block = bytes(range(256)) * 1024
        for _ in range(68):
            output.write(block)
    original = {
        name: engine.tools[name]
        for name in {"files_read", "files_write", "operations_get"} | DOWNLOAD_TOOLS
    }

    async def download(args):
        if (
            not isinstance(args, BeginDownload)
            or args.path != str(binary_source)
            or binary_source.is_symlink()
        ):
            raise ValueError("Probe only permits its generated binary file")
        return await original["download_begin"].handler(args)

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
        **{name: original[name] for name in DOWNLOAD_TOOLS},
        "download_begin": dataclasses.replace(original["download_begin"], handler=download),
    }
    return engine, target


async def stop_probe_tunnel(process, children, drain=None):
    """Stop the owned runner and only child identities observed under that runner."""
    if process and process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 20)
        except TimeoutError:
            process.kill()
            await process.wait()
    # The runner alone owns this stdout pipe; cloudflared uses DEVNULL. Drain its
    # final lifecycle events before freezing the set of observed child identities.
    observation_failed = False
    if drain:
        try:
            await asyncio.wait_for(asyncio.shield(drain), 5)
        except Exception:
            observation_failed = True
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
    for pid, created in list(children.items()):
        try:
            child = psutil.Process(pid)
            if child.create_time() != created:
                continue  # Never act on a reused PID.
            child.terminate()
            try:
                await asyncio.to_thread(child.wait, 5)
            except psutil.TimeoutExpired:
                child.kill()
                await asyncio.to_thread(child.wait, 5)
        except psutil.NoSuchProcess:
            pass
    if observation_failed:
        raise RuntimeError("Connector lifecycle observation did not finish cleanly")


async def cleanup_probe(
    report,
    *,
    remote_client,
    tunnel,
    children,
    drain,
    client_tokens,
    owner_credentials,
    adapter,
    store,
    engine,
):
    """Attempt every cleanup, retaining only error classes and success flags."""

    async def attempt(name, action):
        try:
            await action()
            report[name] = True
        except Exception as error:
            report[name] = False
            report["completed"] = False
            report.setdefault("cleanup_failures", {})[name] = type(error).__name__

    if remote_client:
        await attempt("client_closed", remote_client.close)
    if store:

        async def revoke_grants():
            store.revoke_device(owner="probe-owner", device="probe-device")

        await attempt("cleanup_grants_revoked", revoke_grants)
    if client_tokens:
        await attempt("client_keyring_removed", lambda: asyncio.to_thread(client_tokens.forget))
    if owner_credentials:
        await attempt("owner_keyring_removed", lambda: asyncio.to_thread(owner_credentials.forget))
    await attempt(
        "owned_connector_processes_stopped", lambda: stop_probe_tunnel(tunnel, children, drain)
    )
    if adapter:
        await attempt("adapter_closed", adapter.close)
    if store:
        # SQLite's connection stays on its creating thread.
        async def close_store():
            store.close()

        await attempt("authorization_store_closed", close_store)
    if engine:
        await attempt("engine_closed", engine.close)


async def verify(receipt_path, *, tunnel_directory=None):
    executable = shutil.which("cloudflared")
    if executable is None:
        raise RuntimeError("Install an optional cloudflared test binary before running this probe")
    report = {
        "kind": "constant-public-https" if tunnel_directory else "temporary-public-https",
        "started_at": time.time(),
        "completed": False,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": f"Same {sys.platform} host via public HTTPS edge; disposable file/owner; "
        "HTTP consent form test",
    }
    tunnel = None
    drain = None
    adapter = None
    engine = None
    store = None
    client_tokens = None
    remote_client = None
    owner_credentials = None
    owned_children = {}
    constant = load_probe_tunnel(tunnel_directory) if tunnel_directory else None
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
            port = await adapter.start(constant.port if constant else 0)
            config = directory / "tunnel-config.yml"
            config.write_text("{}\n", encoding="utf-8")
            command = [
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
            ]
            if constant:
                command = [
                    sys.executable,
                    "-m",
                    "anywhere_computer.cli",
                    "tunnel-run",
                    "--state-dir",
                    str(tunnel_directory.resolve()),
                ]
            tunnel = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            found = asyncio.get_running_loop().create_future()

            async def read_tunnel():
                assert tunnel.stdout is not None
                while line := await tunnel.stdout.readline():
                    if constant:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict) and "tunnel_child_started" in event:
                            pid = event["tunnel_child_started"]
                            if type(pid) is not int:
                                continue
                            try:
                                child = psutil.Process(pid)
                                if child.ppid() == tunnel.pid:
                                    owned_children[pid] = child.create_time()
                                    report["latest_tunnel_child"] = pid
                            except psutil.NoSuchProcess:
                                pass
                        continue
                    if b"Registered tunnel connection" in line:
                        report["tunnel_connected"] = True
                    matched = re.search(rb"https://[a-z0-9-]+\.trycloudflare\.com", line)
                    if matched and not found.done():
                        found.set_result(matched.group().decode("ascii"))
                if not constant and not found.done():
                    found.set_exception(
                        RuntimeError("Temporary tunnel exited before publishing URL")
                    )

            drain = asyncio.create_task(read_tunnel())
            public = (
                constant.resource.removesuffix("/mcp")
                if constant
                else (await asyncio.wait_for(found, 60))
            )
            report["public_url"] = public
            print(json.dumps({"stage": "tunnel_created", "public_url": public}), flush=True)
            store = AuthorizationStore(
                directory / "authorization",
                resource=public + "/mcp",
                known_tools=frozenset(engine.tools),
            )
            store.register_client(
                "probe-client",
                frozenset({"http://127.0.0.1/oauth/callback", "http://[::1]/oauth/callback"}),
            )
            store.enroll_device("probe-owner", "probe-device", frozenset(engine.tools))
            backend = AuthorizedDeviceMCP(store, engine, owner="probe-owner", device="probe-device")
            owner_credentials = OwnerCredentials(
                directory / "owner", resource=public + "/mcp", owner="probe-owner"
            )
            owner_password = secrets.token_urlsafe(32)
            await asyncio.to_thread(owner_credentials.initialize, owner_password)
            consent = BrowserAuthorization(store, owner_credentials, device="probe-device")
            oauth = OAuthEndpoints(
                store, authorization_endpoint=consent.authorization_endpoint,
                authorization_response_iss_supported=True,
            )
            adapter.public_routes = {**oauth.routes(), **consent.routes()}
            adapter.origins = frozenset({public})
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
                    status, metadata_headers, body = await asyncio.to_thread(
                        public_request, public + "/.well-known/oauth-protected-resource"
                    )
                    report["last_metadata_status"] = status
                    if status == 200 and body and body.get("resource") == public + "/mcp":
                        require_no_store(metadata_headers)
                        break
                except (OSError, urllib.error.URLError) as error:
                    report["last_network_error"] = type(error).__name__
                if time.monotonic() >= deadline:
                    raise RuntimeError("Public metadata did not become reachable")
                await asyncio.sleep(2)  # GET readiness only; mutations are never retried.
            report["metadata_verified"] = True
            status, _, issuer_metadata = await asyncio.to_thread(
                public_request, public + "/.well-known/oauth-authorization-server",
            )
            if (
                status != 200 or not issuer_metadata
                or issuer_metadata.get("issuer") != public
                or issuer_metadata.get("authorization_response_iss_parameter_supported") is not True
                or body.get("authorization_servers") != [public]
            ):
                raise RuntimeError("Public issuer identification metadata was inconsistent")
            report["issuer_metadata_verified"] = True
            print(json.dumps({"stage": "public_metadata_verified"}), flush=True)
            token_body = None
            native_callback_port = None
            native_callback_host = None

            def simulated_browser(url):
                nonlocal native_callback_port, native_callback_host
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                callback = query["redirect_uri"][0]
                status, form_headers, page = public_request(url, html=True)
                if status != 200 or not isinstance(page, str):
                    raise RuntimeError("Public consent page was unavailable")
                require_no_store(form_headers)
                hidden = dict(re.findall(r"name=(request_id|csrf) value='([^']+)'", page))
                cookie_header = next(
                    value for key, value in form_headers.items() if key.lower() == "set-cookie"
                ).split(";", 1)[0]
                status, consent_headers, _ = public_request(
                    public + "/authorize",
                    method="POST",
                    headers={"Origin": public, "Cookie": cookie_header},
                    form={**hidden, "approve": "yes", "password": owner_password},
                )
                if status != 303:
                    raise RuntimeError("Public owner authentication failed")
                require_no_store(consent_headers)
                location = next(
                    value for key, value in consent_headers.items() if key.lower() == "location"
                )
                returned = urllib.parse.urlsplit(location)
                fields = urllib.parse.parse_qs(returned.query)
                if (
                    returned._replace(query="").geturl() != callback
                    or fields.get("state") != query["state"]
                    or fields.get("iss") != [public]
                    or returned.hostname not in {"127.0.0.1", "::1"}
                    or returned.scheme != "http"
                ):
                    raise RuntimeError("Public consent callback binding failed")
                report["authorization_response_issuer_verified"] = True
                connection = http.client.HTTPConnection(returned.hostname, returned.port, timeout=5)
                try:
                    connection.request("GET", returned.path + "?" + returned.query)
                    response = connection.getresponse()
                    response.read(16384)
                    if response.status != 200:
                        raise RuntimeError("Native callback rejected authorization")
                finally:
                    connection.close()
                native_callback_port = returned.port
                native_callback_host = returned.hostname
                report["owner_password_consent_verified"] = True
                report["browser_rendering_tested"] = False
                return True

            def exchange_code(resource, client, redirect, code, verifier):
                nonlocal token_body
                status, response_headers, token_body = public_request(
                    public + "/oauth/token",
                    method="POST",
                    form={
                        "grant_type": "authorization_code",
                        "code": code,
                        "code_verifier": verifier,
                        "client_id": client,
                        "redirect_uri": redirect,
                        "resource": resource,
                    },
                )
                if status != 200 or not token_body:
                    raise RuntimeError("Public token exchange failed")
                require_no_store(response_headers)
                return TokenReply.model_validate(token_body)

            def refresh_client(resource, client, refresh_token):
                status, response_headers, renewed = public_request(
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
                require_no_store(response_headers)
                return TokenReply.model_validate(renewed)

            client_tokens = ClientTokens(
                directory / "client",
                resource=public + "/mcp",
                client="probe-client",
                profile="disposable-probe",
                refresh=refresh_client,
            )
            await login(
                client_tokens,
                frozenset(engine.tools),
                open_browser=simulated_browser,
                exchange=exchange_code,
            )
            report["native_login_verified"] = True
            if native_callback_port is None or native_callback_host is None or token_body is None:
                raise RuntimeError("Native login did not finish")
            try:
                reader, writer = await asyncio.open_connection(
                    native_callback_host, native_callback_port
                )
            except OSError:
                report["native_callback_closed"] = True
            else:
                writer.close()
                await writer.wait_closed()
                raise RuntimeError("Native callback remained open")
            dropped_write = False
            write_posts = 0
            rejected_after_revocation = False

            def public_wire(resource, method, packet, headers):
                nonlocal dropped_write, write_posts, rejected_after_revocation
                status, response_headers, body = public_request(
                    resource, method=method, payload=packet, headers=headers
                )
                if status in {200, 401}:
                    require_no_store(response_headers)
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

            # Exercise the durable copy via the same public HTTPS client and grant.
            binary_source = directory / "download.bin"
            expected = hashlib.sha256()
            for _ in range(68):
                expected.update(bytes(range(256)) * 1024)
            transfer_id = secrets.token_hex(16)
            prepared = await remote_client.execute(
                Request(
                    operation_id=secrets.token_hex(16),
                    tool="download_begin",
                    arguments={
                        "transfer_id": transfer_id,
                        "path": str(binary_source),
                        "expected_sha256": expected.hexdigest(),
                    },
                )
            )
            if prepared.state != "completed" or prepared.data.get("total_bytes") != 17 * 1024**2:
                raise RuntimeError("Public download preparation failed")
            binary_source.unlink()
            received = hashlib.sha256()
            offset = 0
            download_started = time.monotonic()
            for index in range(68):
                if index == 34:
                    await remote_client.close()
                    remote_client = HTTPBackend(client_tokens, wire=public_wire)
                    resumed = await remote_client.execute(
                        Request(
                            operation_id=secrets.token_hex(16),
                            tool="download_status",
                            arguments={"transfer_id": transfer_id},
                        )
                    )
                    if resumed.state != "completed" or resumed.data.get("state") != "ready":
                        raise RuntimeError("Public client replacement could not resume download")
                    report["download_client_recreated"] = True
                chunk = await remote_client.execute(
                    Request(
                        operation_id=secrets.token_hex(16),
                        tool="download_read",
                        arguments={"transfer_id": transfer_id, "offset": offset},
                    )
                )
                if chunk.state != "completed":
                    raise RuntimeError("Public download chunk failed")
                data = base64.b64decode(chunk.data["data_base64"], validate=True)
                if (
                    len(data) != 262144
                    or hashlib.sha256(data).hexdigest() != chunk.data["chunk_sha256"]
                    or chunk.data["next_offset"] != offset + len(data)
                    or chunk.data["sha256"] != expected.hexdigest()
                ):
                    raise RuntimeError("Public download range or hash mismatch")
                received.update(data)
                offset += len(data)
                if index % 16 == 15:
                    print(
                        json.dumps({"stage": "public_download", "received_bytes": offset}),
                        flush=True,
                    )
            if offset != 17 * 1024**2 or received.hexdigest() != expected.hexdigest():
                raise RuntimeError("Public complete download hash mismatch")
            closed = await remote_client.execute(
                Request(
                    operation_id=secrets.token_hex(16),
                    tool="download_close",
                    arguments={"transfer_id": transfer_id},
                )
            )
            if closed.state != "completed" or closed.data.get("state") != "closed":
                raise RuntimeError("Public download close failed")
            report["public_download_bytes"] = offset
            report["public_download_seconds"] = time.monotonic() - download_started
            report["public_download_sha256_verified"] = True
            report["public_download_closed"] = True

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
            if constant:
                previous_child = report.get("latest_tunnel_child")
                if previous_child not in owned_children:
                    raise RuntimeError("No owned connector identity for restart test")
                child = psutil.Process(previous_child)
                if (
                    child.ppid() != tunnel.pid
                    or child.create_time() != owned_children[previous_child]
                ):
                    raise RuntimeError("Connector ownership changed before restart test")
                prior_session = remote_client.session_id
                recovery_started = time.monotonic()
                child.kill()  # Only this probe runner's observed, identity-checked child.
                report["tunnel_child_crash_injected"] = True
                deadline = time.monotonic() + 90
                while True:
                    if time.monotonic() >= deadline or tunnel.returncode is not None:
                        raise RuntimeError("Constant tunnel did not recover its owned child")
                    replacement = report.get("latest_tunnel_child")
                    if replacement in owned_children and replacement != previous_child:
                        try:
                            status, _, body = await asyncio.to_thread(
                                public_request, public + "/.well-known/oauth-protected-resource"
                            )
                            if status == 200 and body and body.get("resource") == public + "/mcp":
                                break
                        except (OSError, urllib.error.URLError):
                            pass
                    await asyncio.sleep(0.25)
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
                    or remote_client.session_id != prior_session
                ):
                    raise RuntimeError("Authenticated MCP session did not survive tunnel restart")
                report["tunnel_child_crash_recovered"] = True
                report["tunnel_recovery_seconds"] = time.monotonic() - recovery_started
                report["same_authorized_session_after_tunnel_restart"] = True
                print(json.dumps({"stage": "constant_tunnel_restart_verified"}), flush=True)
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
            report["no_store_headers_verified"] = True
            report["completed"] = True
        except Exception as error:
            report["failure_type"] = type(error).__name__
            # Do not persist exception text, requests, tokens, codes, or tunnel logs.
        finally:
            await cleanup_probe(
                report,
                remote_client=remote_client,
                tunnel=tunnel,
                children=owned_children,
                drain=drain,
                client_tokens=client_tokens,
                owner_credentials=owner_credentials,
                adapter=adapter,
                store=store,
                engine=engine,
            )
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
    parser.add_argument(
        "--tunnel-state-dir",
        type=Path,
        help="Use an existing dedicated constant tunnel; performs an owned child crash test",
    )
    arguments = parser.parse_args()
    try:
        asyncio.run(verify(arguments.receipt, tunnel_directory=arguments.tunnel_state_dir))
    except Exception as error:
        print(json.dumps({"completed": False, "failure_type": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
