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
    return _https_enrollment_request(
        endpoint, urlencode(fields).encode("ascii"),
        {"Content-Type": "application/x-www-form-urlencoded"}, context=context, timeout=timeout,
    )


def https_enrollment_registration(
    endpoint: str, fields: dict[str, str], *, token: str,
    context: ssl.SSLContext | None = None, timeout: float = 15,
) -> EnrollmentHTTPReply:
    """Send the saved enrollment grant only to the configured registration endpoint.

    The trusted controller supplies the endpoint and original enrollment ID.
    No redirect, implicit retry, credential persistence or token logging occurs.
    """
    if not token or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Invalid enrollment authorization")
    body = json.dumps(fields, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(body) > 4096:
        raise ValueError("Registration request exceeds limit")
    return _https_enrollment_request(
        endpoint, body, {"Content-Type": "application/json", "Authorization": "Bearer " + token},
        context=context, timeout=timeout,
    )


def _https_enrollment_request(
    endpoint: str, body: bytes, headers: dict[str, str], *,
    context: ssl.SSLContext | None, timeout: float,
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
            **headers,
            "Accept": "application/json", "Connection": "close",
        })
        response = connection.getresponse()
        response_headers: dict[str, str] = {}
        size = 0
        for name, value in response.getheaders():
            size += len(name) + len(value) + 4
            name = name.lower()
            if size > RESPONSE_LIMIT or name in response_headers:
                raise ValueError("Invalid response headers")
            response_headers[name] = value
        media_type = response_headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise ValueError("Expected a JSON response")
        if response_headers.get("content-encoding", "identity").lower() != "identity":
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
