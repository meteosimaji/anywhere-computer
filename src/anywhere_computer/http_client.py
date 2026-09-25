"""Authenticated HTTP connector for the shared Anywhere Computer agent.

Every operation retains its caller-selected ID across the HTTP boundary. Only an
explicit pre-dispatch HTTP rejection may be recovered automatically; a lost or
invalid operation response is never replayed.
"""

import asyncio
import http.client
import json
import re
import ssl
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlsplit

from pydantic import JsonValue

from . import __version__
from .authorization import validate_authorization_url
from .client_tokens import ClientAuthorizationRequired, ClientTokens
from .connection import WIRE_LIMIT
from .http_mcp import AUTH_REJECTED_HEADER, SESSION_EXPIRED_HEADER
from .mcp_server import (
    OPERATION_CAPABILITY,
    OPERATION_META,
    PROTOCOL_VERSION,
    MCPSession,
    serve_stdio,
)
from .models import Reply, Request


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: dict[str, str] = field(repr=False)
    packet: dict[str, JsonValue] | None = field(repr=False)


HTTPWire = Callable[[str, str, dict[str, JsonValue] | None, dict[str, str]], HTTPResponse]


def _json_packet(raw: bytes) -> dict[str, JsonValue]:
    def invalid_constant(value: str) -> None:
        raise ValueError("Non-finite JSON number")

    packet = json.loads(raw, parse_constant=invalid_constant)
    if not isinstance(packet, dict):
        raise ValueError("Expected one JSON-RPC object")
    return cast(dict[str, JsonValue], packet)


def _read_sse(response: http.client.HTTPResponse, identity: JsonValue) -> dict[str, JsonValue]:
    """Read until this request's response, bounded even on a never-ending stream."""
    total = 0
    data: list[bytes] = []
    pending = bytearray()
    after_cr = False
    first_line = True
    while chunk := response.read1(65536):
        total += len(chunk)
        if total > WIRE_LIMIT:
            raise ValueError("SSE response exceeds limit")
        for char in chunk:
            if char == 10 and after_cr:
                after_cr = False
                continue
            after_cr = char == 13
            if char not in {10, 13}:
                pending.append(char)
                continue
            line = bytes(pending)
            pending.clear()
            if first_line:
                line = line.removeprefix(b"\xef\xbb\xbf")
                first_line = False
            if not line:
                if data:
                    raw = b"\n".join(data)
                    data.clear()
                    if not raw:
                        continue  # Empty priming events carry no JSON-RPC message.
                    packet = _json_packet(raw)
                    if packet.get("jsonrpc") != "2.0":
                        raise ValueError("Invalid SSE JSON-RPC message")
                    if "id" in packet:
                        if packet["id"] != identity or "method" in packet:
                            raise ValueError("Unexpected SSE request or response")
                        return packet
                    if not isinstance(packet.get("method"), str):
                        raise ValueError("Invalid SSE notification")
            elif line == b"data":
                data.append(b"")
            elif line.startswith(b"data:"):
                value = line[5:]
                data.append(value[1:] if value.startswith(b" ") else value)
    raise ConnectionError("SSE ended without a response")


def https_mcp_request(
    resource: str, method: str, packet: dict[str, JsonValue] | None, headers: dict[str, str]
) -> HTTPResponse:
    """Single standard-library HTTPS request; no redirects or network retries."""
    validate_authorization_url(resource)
    parsed = urlsplit(resource)
    if parsed.path != "/mcp" or parsed.query or method not in {"POST", "DELETE"}:
        raise ValueError("HTTP connector requires its canonical HTTPS /mcp resource")
    body = json.dumps(packet, allow_nan=False).encode() if packet is not None else None
    if body is not None and len(body) > WIRE_LIMIT:
        raise ValueError("HTTP request exceeds limit")
    connection = http.client.HTTPSConnection(
        parsed.hostname or "", port=parsed.port, timeout=65, context=ssl.create_default_context()
    )
    try:
        connection.request(method, parsed.path, body=body, headers=headers)
        response = connection.getresponse()
        response_headers: dict[str, str] = {}
        header_size = 0
        for name, value in response.getheaders():
            header_size += len(name) + len(value) + 4
            lower = name.lower()
            if header_size > 16384 or lower in response_headers:
                raise ValueError("Invalid or oversized response headers")
            response_headers[lower] = value
        if response.status != 200 or method == "DELETE":
            # Never parse or expose proxy/error pages or authentication response bodies.
            return HTTPResponse(response.status, response_headers, None)
        content_type = response_headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type == "application/json":
            raw = response.read(WIRE_LIMIT + 1)
            if len(raw) > WIRE_LIMIT:
                raise ValueError("HTTP response exceeds limit")
            result = _json_packet(raw)
        elif content_type == "text/event-stream" and packet is not None and "id" in packet:
            result = _read_sse(response, packet["id"])
        else:
            raise ValueError("Unsupported MCP response type")
        return HTTPResponse(response.status, response_headers, result)
    except Exception:
        raise ConnectionError(
            "HTTP response was not confirmed; do not repeat an operation"
        ) from None
    finally:
        connection.close()


class _HTTPRejected(ConnectionError):
    """The trusted agent explicitly rejected the request before dispatch."""


class HTTPBackend:
    def __init__(self, tokens: ClientTokens, *, wire: HTTPWire = https_mcp_request) -> None:
        self.tokens = tokens
        self.wire = wire
        self.session_id: str | None = None
        # Keep this lock in the worker thread: cancelling an asyncio observer must
        # not release it while its actual HTTP operation is still in flight.
        self.lock = threading.RLock()

    def _post(self, packet: dict[str, JsonValue], *, session: str | None) -> HTTPResponse:
        token = self.tokens.access_token()
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session is not None:
            headers["MCP-Session-Id"] = session
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        response = self.wire(self.tokens.resource, "POST", packet, headers)
        if response.status == 401:
            # Only our pre-dispatch marker makes replay of a tool call safe.
            # Initialization and catalog requests can renew without that proof.
            if (packet.get("method") == "tools/call"
                    and response.headers.get(AUTH_REJECTED_HEADER) != "true"):
                return response
            token = self.tokens.access_token(rejected_token=token)
            headers["Authorization"] = "Bearer " + token
            response = self.wire(self.tokens.resource, "POST", packet, headers)
            if response.status == 401:
                if (packet.get("method") == "tools/call"
                        and response.headers.get(AUTH_REJECTED_HEADER) != "true"):
                    return response
                raise ClientAuthorizationRequired(
                    "Remote authorization was rejected; authorize again"
                )
        return response

    @staticmethod
    def _result(response: HTTPResponse, identity: str) -> dict[str, JsonValue]:
        # A gateway can return 503 after the upstream mutation was accepted.
        # A bare server-error status cannot prove non-dispatch.
        if response.status in {400, 403, 404, 405, 406, 413, 415, 429}:
            raise _HTTPRejected(f"Remote request rejected before dispatch (HTTP {response.status})")
        if response.status != 200:
            raise ConnectionError("Remote operation response was not confirmed")
        packet = response.packet
        if (
            packet is None
            or packet.get("jsonrpc") != "2.0"
            or packet.get("id") != identity
            or "method" in packet
            or ("error" in packet) == ("result" in packet)
        ):
            raise ConnectionError("Remote response does not match this request")
        if "error" in packet:
            error = packet["error"]
            if isinstance(error, dict) and error.get("code") in {-32600, -32601, -32602}:
                raise _HTTPRejected("Remote server rejected the MCP request")
            raise ConnectionError("Remote server returned an unconfirmed operation error")
        result = packet.get("result")
        if not isinstance(result, dict):
            raise ConnectionError("Remote response lacks a valid result")
        return result

    @staticmethod
    def _session_expired(response: HTTPResponse) -> bool:
        return (response.status == 404
                and response.headers.get(SESSION_EXPIRED_HEADER) == "true")

    def _ensure_session(self, *, retry_notification: bool = True) -> None:
        if self.session_id is not None:
            return
        identity = uuid.uuid4().hex
        initialized = self._post(
            {
                "jsonrpc": "2.0",
                "id": identity,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "anywhere-computer-http", "version": __version__},
                },
            },
            session=None,
        )
        result = self._result(initialized, identity)
        capabilities = result.get("capabilities")
        experimental = capabilities.get("experimental") if isinstance(capabilities, dict) else None
        extension = (
            experimental.get(OPERATION_CAPABILITY) if isinstance(experimental, dict) else None
        )
        if (
            result.get("protocolVersion") != PROTOCOL_VERSION
            or not isinstance(extension, dict)
            or extension.get("operationId") is not True
        ):
            raise ConnectionError("Remote agent does not support recoverable operation IDs")
        session = initialized.headers.get("mcp-session-id")
        if session is None or re.fullmatch(r"[\x21-\x7e]{1,256}", session) is None:
            raise ConnectionError("Remote agent did not issue a valid session")
        acknowledged = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, session=session
        )
        if self._session_expired(acknowledged) and retry_notification:
            # A session can disappear between initialize and its notification.
            # No tool has been sent yet. Start a fresh handshake once, without ID.
            self._ensure_session(retry_notification=False)
            return
        if acknowledged.status != 202:
            raise ConnectionError("Remote session initialization was not acknowledged")
        self.session_id = session

    def _rpc(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        identity = uuid.uuid4().hex
        packet: dict[str, JsonValue] = {
            "jsonrpc": "2.0",
            "id": identity,
            "method": method,
            "params": params,
        }
        self._ensure_session()
        response = self._post(packet, session=self.session_id)
        if self._session_expired(response):
            # Only the authenticated agent's missing-session marker proves that
            # this request was rejected before tool dispatch.
            self.session_id = None
            self._ensure_session()
            response = self._post(packet, session=self.session_id)
        if (response.status == 404 and method == "tools/call"
                and not self._session_expired(response)):
            raise ConnectionError("Remote tool outcome was not confirmed")
        if (response.status == 401 and method == "tools/call"
                and response.headers.get(AUTH_REJECTED_HEADER) != "true"):
            raise ConnectionError("Remote tool outcome was not confirmed")
        return self._result(response, identity)

    async def catalog(self) -> list[JsonValue]:
        def obtain() -> list[JsonValue]:
            with self.lock:
                try:
                    result = self._rpc("tools/list", {})
                    tools = result.get("tools")
                    if not isinstance(tools, list) or any(
                        not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                        for tool in tools
                    ):
                        raise ConnectionError("Remote catalog is invalid")
                    return tools
                except Exception:
                    self.session_id = None
                    raise

        return await asyncio.to_thread(obtain)

    async def execute(self, request: Request) -> Reply:
        def perform() -> Reply:
            with self.lock:
                try:
                    self._ensure_session()
                except Exception:
                    self.session_id = None
                    return Reply(
                        operation_id=request.operation_id,
                        state="failed",
                        error="Operation was not dispatched: remote connection setup failed",
                    )
                try:
                    result = self._rpc(
                        "tools/call",
                        {
                            "name": request.tool,
                            "arguments": request.arguments,
                            "_meta": {OPERATION_META: request.operation_id},
                        },
                    )
                    reply = Reply.model_validate(result.get("structuredContent"))
                    if reply.operation_id != request.operation_id:
                        raise ConnectionError("Remote operation ID mismatch")
                    return reply
                except (_HTTPRejected, ClientAuthorizationRequired):
                    self.session_id = None
                    return Reply(
                        operation_id=request.operation_id,
                        state="failed",
                        error="Remote request was rejected before execution; check authorization",
                    )
                except Exception:
                    # An unconfirmed operation response does not invalidate the
                    # established MCP session. Retain it for lookup or DELETE;
                    # an actual expired session is handled by the explicit 404 path.
                    return Reply(
                        operation_id=request.operation_id,
                        state="unknown",
                        error="Response lost or invalid. Query operations_get "
                        "with this operation_id; do not repeat the operation.",
                    )

        return await asyncio.to_thread(perform)

    async def close(self) -> None:
        def terminate() -> None:
            with self.lock:
                session, self.session_id = self.session_id, None
                if session is not None:
                    try:
                        self.wire(
                            self.tokens.resource,
                            "DELETE",
                            None,
                            {
                                "Authorization": "Bearer " + self.tokens.access_token(),
                                "MCP-Session-Id": session,
                                "MCP-Protocol-Version": PROTOCOL_VERSION,
                            },
                        )
                    except Exception:
                        pass  # The server also expires idle sessions; never retry a DELETE.

        await asyncio.to_thread(terminate)

    def mcp_session(self) -> MCPSession:
        return MCPSession(self.catalog, self.execute)


async def run_http_mcp(tokens: ClientTokens) -> None:
    backend = HTTPBackend(tokens)
    try:
        # Fail startup with an actionable credential/network error on stderr,
        # instead of advertising a connector whose first catalog silently fails.
        await backend.catalog()
        await serve_stdio(backend.mcp_session(), sys.stdin.buffer, sys.stdout.buffer)
    finally:
        await backend.close()
