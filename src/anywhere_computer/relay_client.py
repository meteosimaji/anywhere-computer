"""Isolated PC-initiated channel lifecycle; never replays an operation."""

import asyncio
import ipaddress
import random
import ssl
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .relay_channels import PC_PROTOCOL
from .relay_grants import AuthorizedRelayAgent, ExecutionRejected
from .remote_transport import FRAME_LIMIT, check_tls


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
                        self.endpoint, ssl=self.context, subprotocols=[PC_PROTOCOL],
                        proxy=None, compression=None, max_size=FRAME_LIMIT, max_queue=1,
                        open_timeout=5, close_timeout=1,
                    ) as socket:
                        self._socket = socket
                        if socket.subprotocol != PC_PROTOCOL:
                            raise ExecutionRejected('Unsupported PC protocol')
                        self.state = 'connected'
                        # Do not reset backoff on handshake alone: a peer that
                        # immediately disconnects must not cause a tight loop.
                        async for payload in socket:
                            if self._stop.is_set():
                                break
                            if not isinstance(payload, bytes):
                                raise ExecutionRejected('Expected binary PC frame')
                            reply = await self.agent.dispatch_frame(payload)
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
