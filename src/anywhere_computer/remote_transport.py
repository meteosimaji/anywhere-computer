"""Bounded, mutually authenticated TLS transport using only the standard library.

This carries internal agent messages, not public HTTP/MCP. TLS contexts and
explicit peer grants must be supplied by the provisioning layer.
"""

import asyncio
import hashlib
import ssl
import struct
from collections.abc import Awaitable, Callable, Mapping

FRAME_LIMIT = 8 * 1024 * 1024
ALPN_PROTOCOL = "anywhere-agent/1"
MessageHandler = Callable[[str, bytes], Awaitable[bytes]]


def check_tls(context: ssl.SSLContext, *, client: bool) -> None:
    if context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("Remote transport requires peer certificate verification")
    if client and not context.check_hostname:
        raise ValueError("Remote clients require hostname verification")
    if context.minimum_version < ssl.TLSVersion.TLSv1_2:
        raise ValueError("TLS 1.2 or later is required")
    if context.keylog_filename is not None:
        raise ValueError("TLS key logging must be disabled")
    context.set_alpn_protocols([ALPN_PROTOCOL])


def peer_fingerprint(writer: asyncio.StreamWriter) -> str:
    channel = writer.get_extra_info("ssl_object")
    if channel is None or channel.selected_alpn_protocol() != ALPN_PROTOCOL:
        raise ConnectionError("Authenticated agent protocol was not negotiated")
    certificate = channel.getpeercert(binary_form=True)
    if not isinstance(certificate, bytes) or not certificate:
        raise ConnectionError("Missing peer certificate")
    return hashlib.sha256(certificate).hexdigest()


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    length = struct.unpack("!I", await reader.readexactly(4))[0]
    if length == 0 or length > FRAME_LIMIT:
        raise ValueError("Invalid remote frame length")
    return await reader.readexactly(length)


async def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    if not payload or len(payload) > FRAME_LIMIT:
        raise ValueError("Invalid remote frame length")
    writer.write(struct.pack("!I", len(payload)) + payload)
    await writer.drain()


async def close_stream(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 2)
    except (OSError, TimeoutError):
        writer.transport.abort()


async def remote_exchange(
    host: str,
    port: int,
    payload: bytes,
    *,
    context: ssl.SSLContext,
    server_name: str,
    expected_fingerprint: str,
    timeout: float = 30,
) -> bytes:
    """One attempt only. Caller retains operation identity after uncertain delivery."""
    check_tls(context, client=True)
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_connection(
            host,
            port,
            ssl=context,
            server_hostname=server_name,
            ssl_handshake_timeout=min(timeout, 10),
            ssl_shutdown_timeout=2,
        )
        try:
            if peer_fingerprint(writer) != expected_fingerprint:
                raise PermissionError("Server certificate is not the enrolled peer")
            await write_frame(writer, payload)
            return await read_frame(reader)
        finally:
            await close_stream(writer)


class RemoteListener:
    def __init__(
        self,
        context: ssl.SSLContext,
        peers: Mapping[str, str],
        handler: MessageHandler,
        *,
        max_connections: int = 32,
        request_timeout: float = 30,
    ) -> None:
        check_tls(context, client=False)
        if max_connections < 1 or request_timeout <= 0:
            raise ValueError("Positive connection and timeout limits are required")
        self.context = context
        # Fingerprints are public enrollment data, not bearer credentials.
        self.peers = dict(peers)
        self.handler = handler
        self.max_connections = max_connections
        self.request_timeout = request_timeout
        self.connections: set[asyncio.Task[None]] = set()
        self.server: asyncio.Server | None = None

    async def start(self, host: str, port: int) -> int:
        if self.server is not None:
            raise RuntimeError("Remote listener already started")
        self.server = await asyncio.start_server(
            self._handle,
            host,
            port,
            ssl=self.context,
            ssl_handshake_timeout=10,
            ssl_shutdown_timeout=2,
        )
        return int(self.server.sockets[0].getsockname()[1])

    def revoke(self, fingerprint: str) -> None:
        self.peers.pop(fingerprint, None)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None:
            await close_stream(writer)
            return
        if len(self.connections) >= self.max_connections:
            await close_stream(writer)
            return
        self.connections.add(task)
        try:
            async with asyncio.timeout(self.request_timeout):
                fingerprint = peer_fingerprint(writer)
                if fingerprint not in self.peers:
                    return
                payload = await read_frame(reader)
                # Recheck after waiting for the frame, so a revoked idle peer cannot dispatch.
                identity = self.peers.get(fingerprint)
                if identity is None:
                    return
                response = await self.handler(identity, payload)
                await write_frame(writer, response)
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
            # Packet contents and certificate material never enter logs.
            pass
        finally:
            await close_stream(writer)
            self.connections.discard(task)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in tuple(self.connections):
            task.cancel()
        await asyncio.gather(*tuple(self.connections), return_exceptions=True)
