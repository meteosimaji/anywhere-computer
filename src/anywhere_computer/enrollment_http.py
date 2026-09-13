"""One bounded HTTPS form request, without redirects or implicit retries."""

import http.client
import json
import socket
import ssl
import threading
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from .authorization import validate_authorization_url

RESPONSE_LIMIT = 16384


class EnrollmentTransportError(ConnectionError):
    def __init__(self, *, dispatched: bool, timeout: bool = False) -> None:
        super().__init__("Enrollment endpoint response could not be confirmed")
        self.dispatched, self.timeout = dispatched, timeout


@dataclass(frozen=True)
class EnrollmentHTTPReply:
    status: int
    fields: dict[str, object] = field(repr=False)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite response number")


def https_enrollment_form(
    endpoint: str, fields: dict[str, str], *, context: ssl.SSLContext | None = None,
    timeout: float = 15,
) -> EnrollmentHTTPReply:
    """Trusted embedding may supply a verified test CA, never disable TLS checks.

    DNS/connect uses the socket timeout. Once connected, a watchdog bounds the
    entire exchange (including slow-drip headers/body), not just each read.
    """
    validate_authorization_url(endpoint)
    parsed = urlsplit(endpoint)
    if parsed.query or not 0 < timeout <= 60:
        raise ValueError("Invalid enrollment endpoint or timeout")
    tls = context if context is not None else ssl.create_default_context()
    if (tls.verify_mode != ssl.CERT_REQUIRED or not tls.check_hostname
            or tls.minimum_version < ssl.TLSVersion.TLSv1_2 or tls.keylog_filename is not None):
        raise ValueError("Enrollment requires verified TLS without key logging")
    body = urlencode(fields).encode("ascii")
    if len(body) > RESPONSE_LIMIT:
        raise ValueError("Enrollment request exceeds limit")
    connection = http.client.HTTPSConnection(
        parsed.hostname or "", port=parsed.port, context=tls, timeout=timeout,
    )
    dispatched = False
    timer: threading.Timer | None = None
    try:
        connection.connect()
        connected = connection.sock
        if connected is None:
            raise ConnectionError("Missing TLS connection")

        def interrupt() -> None:
            try:
                connected.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # Already closed by the completed exchange.

        timer = threading.Timer(timeout, interrupt)
        timer.daemon = True
        timer.start()
        dispatched = True  # A partial write can consume a one-use grant.
        connection.request("POST", parsed.path or "/", body=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json", "Connection": "close",
        })
        response = connection.getresponse()
        headers: dict[str, str] = {}
        size = 0
        for name, value in response.getheaders():
            size += len(name) + len(value) + 4
            name = name.lower()
            if size > RESPONSE_LIMIT or name in headers:
                raise ValueError("Invalid response headers")
            headers[name] = value
        if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ValueError("Expected a JSON response")
        if headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("Compressed enrollment responses are unsupported")
        raw = response.read(RESPONSE_LIMIT + 1)
        if len(raw) > RESPONSE_LIMIT:
            raise ValueError("Enrollment response exceeds limit")
        decoded = json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid_constant)
        if not isinstance(decoded, dict):
            raise ValueError("Expected an object response")
        return EnrollmentHTTPReply(response.status, decoded)
    except (OSError, ValueError, http.client.HTTPException, RecursionError) as error:
        raise EnrollmentTransportError(
            dispatched=dispatched, timeout=isinstance(error, TimeoutError),
        ) from None
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        connection.close()
