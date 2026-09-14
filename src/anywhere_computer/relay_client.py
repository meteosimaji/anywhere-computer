"""Isolated PC-initiated channel lifecycle; never replays an operation."""

import asyncio
import ipaddress
import random
import re
import ssl
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .relay_channels import SIGNED_PC_PROTOCOL
from .relay_grants import AuthorizedRelayAgent, ExecutionRejected
from .remote_transport import FRAME_LIMIT, check_tls


class _EnrollmentConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception | str:
        # Never forward enrollment headers or client identity to a redirect target.
        return exc


class PCEnrollmentReceipt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra='forbid')
    version: Literal[1]
    state: Literal['bound']
    device_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')


class PCRelayClient:
    """One explicitly started client, using provisioned TLS and execution verifier.

    Only loopback development endpoints are accepted. Connection retries do not
    retain requests or restore process state. Authentication/protocol failures
    stop the client instead of triggering an authorization retry loop.
    """

    def __init__(self, endpoint: str, context: ssl.SSLContext,
                 agent: AuthorizedRelayAgent) -> None:
        url = urlsplit(endpoint)
        host = url.hostname or ''
        if host != 'localhost':
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    raise ValueError('Non-loopback relay')
            except ValueError:
                raise ValueError('Isolated PC client requires a loopback endpoint') from None
        if (url.scheme != 'wss' or url.path != '/pc' or url.query or url.fragment
                or url.username is not None or url.password is not None or not url.port):
            raise ValueError('Invalid isolated PC relay endpoint')
        check_tls(context, client=True)
        self.endpoint, self.context, self.agent = endpoint, context, agent
        self.state = 'stopped'
        self._running = False
        self._stop = asyncio.Event()
        self._socket: ClientConnection | None = None

    async def enroll(self, token: str, *, fingerprint: str) -> PCEnrollmentReceipt:
        """Explicit trusted setup; no token persistence or automatic enrollment retry.

        The controller supplies a current vault grant and the certificate's public
        fingerprint. A verified receipt permits a separate run() connection. Lost
        replies remain unconfirmed, even if the server committed the binding.
        """
        if self._running:
            raise RuntimeError('PC relay client is already running')
        if (not token or len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or re.fullmatch(r'[a-f0-9]{64}', fingerprint) is None):
            raise ValueError('Invalid PC enrollment credentials')
        self._running = True
        self.state = 'enrolling'
        try:
            async with asyncio.timeout(10):
                async with _EnrollmentConnect(
                    self.endpoint + '/enroll', ssl=self.context,
                    subprotocols=[SIGNED_PC_PROTOCOL], proxy=None, compression=None,
                    max_size=4096, max_queue=1, open_timeout=5, close_timeout=1,
                    additional_headers={'Authorization': 'Bearer ' + token,
                                        'X-Anywhere-Device': self.agent.device_id},
                ) as socket:
                    if socket.subprotocol != SIGNED_PC_PROTOCOL:
                        raise ValueError('Unsupported enrollment protocol')
                    raw = await socket.recv()
                    if not isinstance(raw, bytes):
                        raise ValueError('Invalid enrollment receipt frame')
                    receipt = PCEnrollmentReceipt.model_validate_json(raw)
                    if (receipt.device_id != self.agent.device_id
                            or receipt.fingerprint != fingerprint):
                        raise ValueError('PC enrollment receipt does not match')
                    self.state = 'enrolled'
                    return receipt
        except asyncio.CancelledError:
            self.state = 'failed'
            raise
        except Exception:
            self.state = 'failed'
            raise RuntimeError('PC enrollment was not confirmed; inspect before retrying') from None
        finally:
            self._running = False

    async def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            await self._socket.close(code=1000, reason='PC client stopped')

    async def run(self) -> None:
        if self._running:
            raise RuntimeError('PC relay client is already running')
        self._running = True
        self._stop.clear()
        failures = 0
        try:
            while not self._stop.is_set():
                self.state = 'connecting'
                try:
                    async with connect(
                        self.endpoint, ssl=self.context, subprotocols=[SIGNED_PC_PROTOCOL],
                        proxy=None, compression=None, max_size=FRAME_LIMIT, max_queue=1,
                        open_timeout=5, close_timeout=1,
                    ) as socket:
                        self._socket = socket
                        # stop() may have run while connect was awaiting the
                        # handshake and no socket was available to close yet.
                        if self._stop.is_set():
                            break
                        if socket.subprotocol != SIGNED_PC_PROTOCOL:
                            raise ExecutionRejected('Unsupported PC protocol')
                        self.state = 'connected'
                        # Do not reset backoff on handshake alone: a peer that
                        # immediately disconnects must not cause a tight loop.
                        async for payload in socket:
                            if self._stop.is_set():
                                break
                            if not isinstance(payload, bytes):
                                raise ExecutionRejected('Expected binary PC frame')
                            try:
                                reply = await self.agent.dispatch_frame(payload)
                            except (OSError, TimeoutError):
                                # These came from local execution, not the socket.
                                # Reconnecting cannot repair an engine/storage failure.
                                raise RuntimeError('PC execution failed; outcome unknown') from None
                            await socket.send(reply)
                            failures = 0
                        break  # Normal close/replacement requires explicit restart.
                except ssl.SSLError:
                    self.state = 'failed'
                    raise
                except ConnectionClosed as error:
                    code = error.rcvd.code if error.rcvd is not None else 1006
                    if code not in (1006, 1012, 1013):
                        self.state = 'failed'
                        raise
                except (OSError, TimeoutError):
                    pass  # Transient transport failure, with no retained request.
                finally:
                    self._socket = None
                self.state = 'reconnecting'
                delay = random.uniform(0.25, min(30.0, 2.0 ** min(failures, 5)))
                failures += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        except Exception:
            self.state = 'failed'
            raise
        finally:
            self._running = False
            if self.state != 'failed':
                self.state = 'stopped'
