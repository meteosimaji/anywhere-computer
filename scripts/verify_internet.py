"""Explicit, temporary public-HTTPS probe; requires an existing cloudflared binary.

Only one disposable text file is exposed. No terminal, owner files, production
agent, keyring credentials, background service or permanent tunnel is used.
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
from anywhere_computer.engine import Engine
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.models import ReadFile, WriteFile
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
    original = {name: engine.tools[name] for name in ("files_read", "files_write")}

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
            token = token_body["access_token"]
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            }

            async def rpc(method, params=None, identity=1):
                packet = {"jsonrpc": "2.0", "method": method}
                if identity is not None:
                    packet["id"] = identity
                if params is not None:
                    packet["params"] = params
                return await asyncio.to_thread(
                    public_request, public + "/mcp", method="POST", payload=packet, headers=headers
                )

            async def initialize():
                status, response_headers, response = await rpc(
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "internet-probe", "version": "1"},
                    },
                )
                if status != 200 or not response or "result" not in response:
                    raise RuntimeError("Public MCP initialization failed")
                normalized = {key.lower(): value for key, value in response_headers.items()}
                headers["MCP-Session-Id"] = normalized["mcp-session-id"]
                if (await rpc("notifications/initialized", identity=None))[0] != 202:
                    raise RuntimeError("Public MCP notification failed")

            await initialize()
            status, _, catalog = await rpc("tools/list")
            if status != 200 or {tool["name"] for tool in catalog["result"]["tools"]} != set(
                engine.tools
            ):
                raise RuntimeError("Unexpected public tool exposure")
            text = "Anywhere Computer インターネット接続試験 " + secrets.token_hex(8)
            status, _, written = await rpc(
                "tools/call",
                {"name": "files_write", "arguments": {"path": str(target), "text": text}},
            )
            if status != 200 or written["result"]["structuredContent"]["state"] != "completed":
                raise RuntimeError("Public file write failed")
            # A new MCP session, same persistent engine and file; no write is retried.
            headers.pop("MCP-Session-Id")
            await initialize()
            status, _, read = await rpc(
                "tools/call", {"name": "files_read", "arguments": {"path": str(target)}}
            )
            if status != 200 or read["result"]["structuredContent"]["data"]["text"] != text:
                raise RuntimeError("Public file read did not match the written content")
            report["write_read_across_sessions"] = True
            store.revoke_device(owner="probe-owner", device="probe-device")
            if (await rpc("tools/list"))[0] != 401:
                raise RuntimeError("Public request remained authorized after device revocation")
            report["revocation_verified"] = True
            report["completed"] = True
        except Exception as error:
            report["failure_type"] = type(error).__name__
            # Do not persist exception text, requests, tokens, codes, or tunnel logs.
            raise
        finally:
            if tunnel and tunnel.returncode is None:
                tunnel.terminate()
                try:
                    await asyncio.wait_for(tunnel.wait(), 10)
                except TimeoutError:
                    tunnel.kill()
                    await tunnel.wait()
            if drain:
                await asyncio.gather(drain, return_exceptions=True)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=Path("dist/internet-verification.json"))
    arguments = parser.parse_args()
    try:
        asyncio.run(verify(arguments.receipt))
    except Exception as error:
        print(json.dumps({"completed": False, "failure_type": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
