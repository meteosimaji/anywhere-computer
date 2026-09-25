"""Bounded loopback Streamable HTTP adapter with mandatory identity verification.

JSON responses only; SSE is optional and not offered. This is a transport
building block, not an OAuth issuer or an internet deployment command.
"""

import asyncio
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from http import HTTPStatus
from typing import cast
from urllib.parse import urlsplit

from pydantic import JsonValue

from .connection import WIRE_LIMIT
from .mcp_server import OPERATION_META, PROTOCOL_VERSION, MCPSession, _reply_result, rpc_error
from .models import Reply

Authenticate = Callable[[str], Awaitable[str | None]]
SessionFactory = Callable[[str], MCPSession]
HTTPResult = tuple[int, dict[str, JsonValue] | bytes | None, dict[str, str]]
HTTPRoute = Callable[[str, dict[str, str], bytes, str], Awaitable[HTTPResult]]
HEADER_LIMIT = 16384
SESSION_EXPIRED_HEADER = "x-anywhere-mcp-session-expired"
AUTH_REJECTED_HEADER = "x-anywhere-mcp-auth-rejected"
DISPATCH_RECEIPT_WAIT = 45.0


@dataclass
class HTTPSession:
    owner: str
    protocol: MCPSession
    touched: float


class HTTPFailure(Exception):
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}


class HTTPMCP:
    def __init__(
        self,
        authenticate: Authenticate,
        session_factory: SessionFactory,
        *,
        origins: frozenset[str] = frozenset(),
        session_ttl: float = 1800,
        max_sessions: int = 128,
        public_routes: dict[str, HTTPRoute] | None = None,
        auth_challenge: str = "Bearer",
    ) -> None:
        if session_ttl <= 0 or max_sessions < 1:
            raise ValueError("Session bounds must be positive")
        self._bearer: ContextVar[tuple[asyncio.Task[object] | None, str] | None] = (
            ContextVar("http_mcp_bearer", default=None)
        )
        self.authenticate = authenticate
        self.session_factory = session_factory
        self.origins = origins
        self.session_ttl = session_ttl
        self.max_sessions = max_sessions
        # Discovery/token routes are public: handlers enforce their own input/auth contracts.
        # Never mount a privileged operation here expecting MCP bearer authentication.
        self.public_routes = dict(public_routes or {})
        if "/mcp" in self.public_routes or any(
            not path.startswith("/") or "?" in path or "#" in path for path in self.public_routes
        ):
            raise ValueError("Invalid HTTP extension route")
        if not auth_challenge.strip() or any(
            ord(char) < 32 or ord(char) > 126 for char in auth_challenge
        ):
            raise ValueError("Invalid authentication challenge")
        self.auth_challenge = auth_challenge
        self.sessions: dict[str, HTTPSession] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.dispatch_tasks: set[asyncio.Task[tuple[int, dict[str, JsonValue] | None,
                                                  dict[str, str]]]] = set()
        self.hosts: frozenset[str] = frozenset()
        self.server: asyncio.Server | None = None

    async def start(self, port: int = 0) -> int:
        if self.server is not None:
            raise RuntimeError("HTTP adapter is already started")
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", port, limit=HEADER_LIMIT
        )
        bound_port = int(self.server.sockets[0].getsockname()[1])
        self.hosts = frozenset({f"127.0.0.1:{bound_port}", f"localhost:{bound_port}"})
        return bound_port

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.dispatch_tasks:
            await asyncio.gather(*list(self.dispatch_tasks), return_exceptions=True)
        self.sessions.clear()

    async def _read(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
        raw = await reader.readuntil(b"\r\n\r\n")
        if len(raw) > HEADER_LIMIT:
            raise HTTPFailure(431)
        lines = raw[:-4].decode("ascii").split("\r\n")
        request_line = lines.pop(0).split(" ")
        if len(request_line) != 3 or request_line[2] != "HTTP/1.1":
            raise HTTPFailure(400)
        method, target, _ = request_line
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment or not target.startswith("/"):
            raise HTTPFailure(400)
        if parsed.path != "/mcp" and parsed.path not in self.public_routes:
            raise HTTPFailure(404)
        if parsed.path == "/mcp" and parsed.query:
            raise HTTPFailure(400)
        headers: dict[str, str] = {}
        for line in lines:
            name, separator, value = line.partition(":")
            if not separator or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is None:
                raise HTTPFailure(400)
            name = name.lower()
            value = value.strip(" \t")
            if name in headers or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise HTTPFailure(400)
            headers[name] = value
        if headers.get("host") not in self.hosts:
            raise HTTPFailure(403)
        if "origin" in headers and headers["origin"] not in self.origins:
            raise HTTPFailure(403)
        # Exactly one framing mechanism; never let a proxy and this endpoint disagree.
        if "transfer-encoding" in headers or "expect" in headers:
            raise HTTPFailure(400)
        length = headers.get("content-length", "0")
        if re.fullmatch(r"[0-9]{1,10}", length) is None:
            raise HTTPFailure(400)
        size = int(length)
        if size > WIRE_LIMIT:
            raise HTTPFailure(413)
        if method != "POST" and size:
            raise HTTPFailure(400)
        body = await reader.readexactly(size)
        return method, target, headers, body

    async def _dispatch(
        self, method: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, JsonValue] | None, dict[str, str]]:
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or " " in token:
            raise HTTPFailure(401, {AUTH_REJECTED_HEADER: "true"})
        # Called after reading the entire body, including for existing sessions and DELETE.
        async with asyncio.timeout(10):
            owner = await self.authenticate(token)
        if not owner:
            raise HTTPFailure(401, {AUTH_REJECTED_HEADER: "true"})
        receipt = self._recoverable_call(body)
        if method == "POST" and receipt is not None:
            identity, name, operation_id = receipt
            if len(self.dispatch_tasks) >= 32:
                raise HTTPFailure(503)

            async def dispatched() -> tuple[int, dict[str, JsonValue] | None, dict[str, str]]:
                marker = self._bearer.set((asyncio.current_task(), token))
                try:
                    return await self._dispatch_authenticated(method, headers, body, owner)
                finally:
                    self._bearer.reset(marker)

            task = asyncio.create_task(dispatched())
            self.dispatch_tasks.add(task)

            def completed(done: asyncio.Task[tuple[int, dict[str, JsonValue] | None,
                                                 dict[str, str]]]) -> None:
                self.dispatch_tasks.discard(done)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(completed)
            try:
                return await asyncio.wait_for(asyncio.shield(task), DISPATCH_RECEIPT_WAIT)
            except TimeoutError:
                if name.startswith("subchat_"):
                    next_action = (
                        "Use subchat_list to locate the saved submission ID, then "
                        "subchat_status or subchat_recover. The transport request ID may "
                        "differ from the saved ID. A missing row is inconclusive while "
                        "this call is processing. Do not repeat the tool call."
                    )
                else:
                    next_action = (
                        "Poll operations_get with this operation_id using the same "
                        "authorization grant. Do not repeat the tool call."
                    )
                pending = Reply(
                    operation_id=operation_id, state="running",
                    data={
                        "result_pending": True,
                        "next_action": next_action,
                    },
                )
                return 200, {"jsonrpc": "2.0", "id": identity,
                             "result": _reply_result(name, pending)}, {}
        marker = self._bearer.set((asyncio.current_task(), token))
        try:
            return await self._dispatch_authenticated(method, headers, body, owner)
        finally:
            self._bearer.reset(marker)

    @staticmethod
    def _recoverable_call(body: bytes) -> tuple[str | int, str, str] | None:
        try:
            packet = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            return None
        if (not isinstance(packet, dict) or packet.get("jsonrpc") != "2.0"
                or packet.get("method") != "tools/call"):
            return None
        identity = packet.get("id")
        params = packet.get("params")
        if (isinstance(identity, bool) or not isinstance(identity, (str, int))
                or not isinstance(params, dict) or not isinstance(params.get("name"), str)):
            return None
        arguments, metadata = params.get("arguments", {}), params.get("_meta", {})
        if not isinstance(arguments, dict) or not isinstance(metadata, dict):
            return None
        supplied, meta_id = arguments.get("request_id"), metadata.get(OPERATION_META)
        if supplied is not None and meta_id is not None and supplied != meta_id:
            return None
        operation_id = supplied if supplied is not None else meta_id
        if not isinstance(operation_id, str) or re.fullmatch(r"[a-f0-9]{32}", operation_id) is None:
            return None
        return identity, params["name"], operation_id

    def current_bearer(self) -> str:
        """Fresh authenticated token, available only in the handling task."""
        context = self._bearer.get()
        if context is None or context[0] is not asyncio.current_task():
            raise RuntimeError("No authenticated HTTP request is active")
        return context[1]

    async def _dispatch_authenticated(
        self, method: str, headers: dict[str, str], body: bytes, owner: str,
    ) -> tuple[int, dict[str, JsonValue] | None, dict[str, str]]:
        now = time.monotonic()
        self.sessions = {
            key: value
            for key, value in self.sessions.items()
            if now - value.touched < self.session_ttl
        }
        session_id = headers.get("mcp-session-id")
        session = self.sessions.get(session_id) if session_id else None
        if session_id and (session is None or session.owner != owner):
            # This authenticated rejection occurs before protocol.handle. It is
            # the only 404 for which a client may safely retry a tool call.
            raise HTTPFailure(404, {SESSION_EXPIRED_HEADER: "true"})
        if headers.get("mcp-protocol-version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            raise HTTPFailure(400)
        if method == "GET":
            return 405, None, {"Allow": "POST, DELETE"}
        if method == "DELETE":
            if session_id is None:
                raise HTTPFailure(400)
            self.sessions.pop(session_id, None)
            return 200, None, {}
        if method != "POST":
            return 405, None, {"Allow": "POST, DELETE"}
        accepted = {value.strip().split(";")[0] for value in headers.get("accept", "").split(",")}
        if not {"application/json", "text/event-stream"} <= accepted:
            raise HTTPFailure(406)
        if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPFailure(415)
        try:
            packet = json.loads(body.decode("utf-8"), parse_constant=self._invalid_constant)
        except (ValueError, UnicodeError):
            return 400, rpc_error(None, -32700, "Invalid JSON"), {}
        if not isinstance(packet, dict):
            return 400, rpc_error(None, -32600, "Expected one JSON-RPC message"), {}
        if "method" not in packet:
            # No server requests are issued, so there is no outstanding response to accept.
            return 400, rpc_error(None, -32600, "Unsolicited JSON-RPC response"), {}
        initializing = packet.get("method") == "initialize" and "id" in packet
        fresh = False
        if session is None:
            if not initializing:
                raise HTTPFailure(400)
            if len(self.sessions) >= self.max_sessions:
                raise HTTPFailure(503)
            session_id = secrets.token_urlsafe(32)
            session = HTTPSession(owner, self.session_factory(owner), now)
            self.sessions[session_id] = session
            fresh = True
        session.touched = now
        try:
            response = await session.protocol.handle(cast(JsonValue, packet))
        except Exception:
            response = rpc_error(packet.get("id"), -32603, "Internal error")
        response_headers: dict[str, str] = {}
        if fresh:
            if response is not None and "result" in response and session.protocol.initialized:
                assert session_id is not None
                response_headers["MCP-Session-Id"] = session_id
            else:
                self.sessions.pop(str(session_id), None)
        return (202 if response is None else 200), response, response_headers

    @staticmethod
    def _invalid_constant(value: str) -> None:
        raise ValueError("Non-finite JSON number")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None:
            writer.close()
            return
        if len(self.tasks) >= 32:
            writer.close()
            await writer.wait_closed()
            return
        self.tasks.add(task)
        try:
            try:
                async with asyncio.timeout(10):
                    method, target, headers, body = await self._read(reader)
                parsed = urlsplit(target)
                async with asyncio.timeout(30 if parsed.path in self.public_routes else 60):
                    if parsed.path in self.public_routes:
                        status, response, extra = await self.public_routes[parsed.path](
                            method, headers, body, parsed.query
                        )
                    else:
                        status, response, extra = await self._dispatch(method, headers, body)
            except HTTPFailure as error:
                status, response, extra = error.status, None, dict(error.headers)
                if status == 401:
                    extra["WWW-Authenticate"] = self.auth_challenge
            except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                status, response, extra = 400, None, {}
            except TimeoutError:
                status, response, extra = 408, None, {}
            except Exception:
                status, response, extra = 500, None, {}
            payload = (
                response
                if isinstance(response, bytes)
                else (
                    json.dumps(response, ensure_ascii=False, allow_nan=False).encode()
                    if response is not None
                    else b""
                )
            )
            if len(payload) > WIRE_LIMIT:
                status, payload, extra = 500, b"", {}
            response_headers = {
                "Content-Length": str(len(payload)),
                "Connection": "close",
                "Cache-Control": "no-store",
                **extra,
            }
            if payload and not isinstance(response, bytes):
                response_headers["Content-Type"] = "application/json"
            head = f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
            head += "".join(f"{name}: {value}\r\n" for name, value in response_headers.items())
            writer.write(head.encode("ascii") + b"\r\n" + payload)
            await asyncio.wait_for(writer.drain(), 5)
        except (OSError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 5)
            except (OSError, TimeoutError):
                pass
            self.tasks.discard(task)
