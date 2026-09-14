"""Isolated outbound PC channels, not an AI authorization or public MCP API.

Only trusted relay code may call exchange after verifying an AI grant. The PC
must independently verify that grant before executing an operation. This module
only authenticates the PC connection and binds it to durable device ownership.
"""

import asyncio
import hashlib
import ipaddress
import json
import ssl
from dataclasses import dataclass, field

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.typing import Subprotocol

from .models import Reply, Request
from .relay_enrollment import RelayEnrollment
from .relay_grants import ExecutionEnvelope, ExecutionVerifier
from .relay_registry import RelayAccount, RelayRegistry
from .remote_transport import FRAME_LIMIT, check_tls

PC_PROTOCOL = Subprotocol('anywhere-pc.v1')
SIGNED_PC_PROTOCOL = Subprotocol('anywhere-pc.signed.v1')


class ChannelUnavailable(RuntimeError):
    """No request was dispatched by this call."""


class ChannelOutcomeUnknown(RuntimeError):
    """A dispatched request has no verified reply; never automatically replay it."""


@dataclass
class _Channel:
    socket: ServerConnection
    fingerprint: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RelayChannels:
    """One current connection and one in-flight request per registered PC.

    All methods and the registry must be used on the same event-loop thread.
    There is no offline queue, reconnect loop, engine or persistent result copy.
    """

    def __init__(self, registry: RelayRegistry, context: ssl.SSLContext, *,
                 max_connections: int = 32, enrollment: RelayEnrollment | None = None) -> None:
        check_tls(context, client=False)
        context.set_alpn_protocols(['http/1.1'])
        if not 1 <= max_connections <= 256:
            raise ValueError('Invalid PC channel capacity')
        self.enrollment = enrollment
        self.registry = registry
        self.context = context
        self.max_connections = max_connections
        self._channels: dict[str, _Channel] = {}
        self._server: Server | None = None

    async def start(self, host: str = '127.0.0.1', port: int = 0) -> int:
        if self._server is not None:
            raise RuntimeError('PC relay already started')
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError('This isolated PC relay requires a loopback listener')
        self._server = await serve(
            self._accept, host, port, ssl=self.context, origins=[None],
            subprotocols=[SIGNED_PC_PROTOCOL, PC_PROTOCOL], compression=None, max_size=FRAME_LIMIT,
            max_queue=1, open_timeout=5, close_timeout=1,
        )
        return int(self._server.sockets[0].getsockname()[1])

    async def _accept(self, socket: ServerConnection) -> None:
        if (socket.subprotocol not in {PC_PROTOCOL, SIGNED_PC_PROTOCOL} or socket.request is None
                or socket.request.path not in {'/pc', '/pc/enroll'}):
            await socket.close(code=1008, reason='Unsupported PC channel')
            return
        tls = socket.transport.get_extra_info('ssl_object')
        certificate = tls.getpeercert(binary_form=True) if tls is not None else None
        if not isinstance(certificate, bytes) or not certificate:
            await socket.close(code=1008, reason='PC certificate required')
            return
        fingerprint = hashlib.sha256(certificate).hexdigest()
        try:
            if socket.request.path == '/pc/enroll':
                if self.enrollment is None or socket.subprotocol != SIGNED_PC_PROTOCOL:
                    raise ValueError('Enrollment is not configured')
                headers = socket.request.headers
                scheme, _, token = headers.get('Authorization', '').partition(' ')
                device_id = headers.get('X-Anywhere-Device', '')
                if (scheme.lower() != 'bearer' or not token or len(token) > 8192
                        or any(c.isspace() for c in token)):
                    raise ValueError('Invalid enrollment header')
                self.enrollment.bind_channel(token, device_id=device_id,
                                             peer_fingerprint=fingerprint)
                await socket.send(json.dumps({
                    'version': 1, 'state': 'bound', 'device_id': device_id,
                    'fingerprint': fingerprint,
                }).encode())
                await socket.close(code=1000, reason='PC enrollment complete')
                return
            _, device = self.registry.channel_device(fingerprint)
        except (ValueError, LookupError):
            await socket.close(code=1008, reason='PC registration unavailable')
            return
        previous = self._channels.get(device.device_id)
        if previous is None and len(self._channels) >= self.max_connections:
            await socket.close(code=1013, reason='PC channel capacity reached')
            return
        current = _Channel(socket, fingerprint)
        self._channels[device.device_id] = current
        try:
            if previous is not None:
                await previous.socket.close(code=1001, reason='PC connection replaced')
            await socket.wait_closed()
        finally:
            # An old connection finishing must never remove its replacement.
            if self._channels.get(device.device_id) is current:
                del self._channels[device.device_id]

    def _owned_channel(self, account: RelayAccount, device_id: str) -> _Channel:
        channel = self._channels.get(device_id)
        if channel is None:
            raise ChannelUnavailable('PC is offline; request was not dispatched')
        try:
            actual, device = self.registry.channel_device(channel.fingerprint)
        except ValueError:
            raise ChannelUnavailable('PC registration unavailable') from None
        if actual != account or device.device_id != device_id:
            raise ChannelUnavailable('PC registration unavailable')
        return channel

    async def exchange(self, account: RelayAccount, device_id: str, request: Request,
                       *, timeout: float = 65) -> Reply:
        """Trusted internal caller only; account construction is not authentication."""
        return await self._exchange_payload(account, device_id, request,
                                             request.model_dump_json().encode(),
                                             protocol=PC_PROTOCOL, timeout=timeout)

    async def exchange_authorized(self, verifier: ExecutionVerifier, account: RelayAccount,
                                  device_id: str, token: str, request: Request,
                                  *, timeout: float = 65) -> Reply:
        """Verify before transport; the receiving PC verifies the envelope again.

        account must come from the authenticated AI connection, not its body.
        No registration token, unsigned claims or synthetic permissions are added.
        """
        verifier.verify(token, account=account, device_id=device_id, tool=request.tool)
        payload = ExecutionEnvelope(token=token, request=request).model_dump_json().encode()
        reply = await self._exchange_payload(account, device_id, request, payload,
                                             protocol=SIGNED_PC_PROTOCOL, timeout=timeout)
        try:
            verifier.verify(token, account=account, device_id=device_id, tool=request.tool)
        except ValueError:
            raise ChannelOutcomeUnknown('Authorization changed; PC result was withheld') from None
        return reply

    async def _exchange_payload(self, account: RelayAccount, device_id: str, request: Request,
                                payload: bytes, *, protocol: Subprotocol, timeout: float) -> Reply:
        if not 0 < timeout <= 120:
            raise ValueError('Invalid PC exchange timeout')
        if len(payload) > FRAME_LIMIT:
            raise ValueError('PC request exceeds frame limit')
        channel = self._owned_channel(account, device_id)
        if channel.socket.subprotocol != protocol:
            raise ChannelUnavailable(
                "PC protocol does not support this request; request was not dispatched",
            )
        if channel.lock.locked():
            raise ChannelUnavailable('PC is busy; request was not dispatched')
        async with channel.lock:
            try:
                async with asyncio.timeout(timeout):
                    await channel.socket.send(payload)
                    raw = await channel.socket.recv()
                    if not isinstance(raw, bytes):
                        raise ValueError('PC response must be a binary frame')
                    reply = Reply.model_validate_json(raw)
                    if reply.operation_id != request.operation_id:
                        raise ValueError('PC response identity mismatch')
                    if self._owned_channel(account, device_id) is not channel:
                        raise ValueError('PC connection was replaced')
                    return reply
            except asyncio.CancelledError:
                await channel.socket.close(code=1001, reason='PC exchange cancelled')
                raise
            except Exception:
                await channel.socket.close(code=1001, reason='PC reply unavailable')
                raise ChannelOutcomeUnknown(
                    'PC outcome unknown; recover the original operation after reconnect',
                ) from None

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
