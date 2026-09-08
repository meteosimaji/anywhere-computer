"""Native browser authorization with a temporary, bounded IP-loopback callback."""

import asyncio
import hmac
import http.client
import re
import secrets
import ssl
import time
import webbrowser
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .authorization import pkce_s256, validate_authorization_url
from .client_tokens import ClientAuthorizationRequired, ClientTokens, TokenReply


def https_code_exchange(
    resource: str, client: str, redirect: str, code: str, verifier: str
) -> TokenReply:
    """Redeem once; an uncertain exchange must be replaced by fresh authorization."""
    validate_authorization_url(resource)
    parsed = urlsplit(resource)
    if parsed.scheme != "https" or parsed.path != "/mcp" or parsed.query:
        raise ValueError("Login requires the canonical HTTPS /mcp resource")
    connection = http.client.HTTPSConnection(
        parsed.hostname or "", port=parsed.port, timeout=15, context=ssl.create_default_context()
    )
    try:
        connection.request(
            "POST",
            "/oauth/token",
            body=urlencode(
                {
                    "grant_type": "authorization_code",
                    "client_id": client,
                    "redirect_uri": redirect,
                    "resource": resource,
                    "code": code,
                    "code_verifier": verifier,
                }
            ).encode("ascii"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        raw = response.read(16385)
        if (
            response.status != 200
            or len(raw) > 16384
            or response.getheader("Content-Type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            raise ValueError("Unconfirmed token response")
        return TokenReply.model_validate_json(raw)
    except Exception:
        raise ClientAuthorizationRequired(
            "Authorization exchange was not confirmed; start login again"
        ) from None
    finally:
        connection.close()


class _LoopbackCallback:
    def __init__(self, state: str, issuer: str) -> None:
        self.state, self.issuer = state, issuer
        self.result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.port = 0
        self.host = "127.0.0.1"
        self.authority = ""
        self.consumed = False

    async def start(self) -> str:
        try:
            self.server = await asyncio.start_server(self._accept, self.host, 0, limit=8192)
        except OSError:
            self.host = "::1"
            self.server = await asyncio.start_server(self._accept, self.host, 0, limit=8192)
        self.port = int(self.server.sockets[0].getsockname()[1])
        host = "[::1]" if self.host == "::1" else self.host
        self.authority = f"{host}:{self.port}"
        return f"http://{self.authority}/oauth/callback"

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self.tasks) >= 8:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        code: str | None = None
        denied = False
        try:
            async with asyncio.timeout(5):
                try:
                    raw = await reader.readuntil(b"\r\n\r\n")
                    if len(raw) > 8192:
                        raise ValueError("Oversized callback")
                    lines = raw.decode("ascii").split("\r\n")
                    method, target, version = lines.pop(0).split(" ")
                    if method != "GET" or version != "HTTP/1.1" or len(lines) > 24:
                        raise ValueError("Invalid callback request")
                    headers = {}
                    for line in lines:
                        if not line:
                            continue
                        name, separator, value = line.partition(":")
                        name = name.lower()
                        if (
                            not separator
                            or name in headers
                            or re.fullmatch(r"[a-z0-9-]+", name) is None
                            or any(ord(c) < 32 or ord(c) > 126 for c in value)
                        ):
                            raise ValueError("Invalid callback header")
                        headers[name] = value.strip()
                    if (
                        headers.get("host") != self.authority
                        or headers.get("content-length", "0") != "0"
                        or "transfer-encoding" in headers
                        or "origin" in headers
                    ):
                        raise ValueError("Invalid callback framing")
                    parsed = urlsplit(target)
                    if (
                        parsed.scheme
                        or parsed.netloc
                        or parsed.fragment
                        or parsed.path != "/oauth/callback"
                        or re.search(r"%(?![A-Fa-f0-9]{2})", parsed.query)
                    ):
                        raise ValueError("Invalid callback URL")
                    pairs = parse_qsl(
                        parsed.query,
                        keep_blank_values=True,
                        strict_parsing=True,
                        encoding="utf-8",
                        errors="strict",
                        max_num_fields=8,
                    )
                    fields = dict(pairs)
                    if (
                        len(fields) != len(pairs)
                        or not hmac.compare_digest(
                            fields.get("state", "").encode(), self.state.encode()
                        )
                        or ("iss" in fields and fields["iss"] != self.issuer)
                        or ("code" in fields) == ("error" in fields)
                        or self.consumed
                    ):
                        raise ValueError("Invalid callback binding")
                    if "code" in fields:
                        candidate = fields["code"]
                        if re.fullmatch(r"[\x21-\x7e]{1,256}", candidate) is None:
                            raise ValueError("Invalid authorization code")
                        code = candidate
                    else:
                        denied = True
                    self.consumed = True
                    status = "200 OK"
                    message = "接続要求を受け取りました。端末の設定画面に戻ってください。"
                except (ValueError, UnicodeError, asyncio.LimitOverrunError):
                    status = "400 Bad Request"
                    message = "この接続要求は確認できません。"
                body = (
                    "<!doctype html><html lang=ja><meta charset=utf-8>"
                    "<title>Anywhere Computer</title><p>" + message + "</p></html>"
                ).encode()
                writer.write(
                    (
                        f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
                        "Content-Type: text/html; charset=utf-8\r\nConnection: close\r\n"
                        "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                        "Content-Security-Policy: default-src 'none'; frame-ancestors 'none'\r\n"
                        "X-Content-Type-Options: nosniff\r\n\r\n"
                    ).encode()
                    + body
                )
                await writer.drain()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            # A closed browser connection must not strand an already validated decision.
            if not self.result.done():
                if code is not None:
                    self.result.set_result(code)
                elif denied:
                    self.result.set_exception(
                        ClientAuthorizationRequired("Connection was not approved")
                    )
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not self.result.done():
            self.result.cancel()
        elif not self.result.cancelled():
            self.result.exception()  # Retrieve a denial even if browser launch failed.


async def login(
    tokens: ClientTokens,
    scopes: frozenset[str],
    *,
    open_browser: Callable[[str], bool] = webbrowser.open,
    exchange: Callable[[str, str, str, str, str], TokenReply] = https_code_exchange,
    timeout: float = 300,
) -> None:
    """Authenticate to this product's colocated issuer; never print codes or tokens."""
    if (
        not scopes
        or len(scopes) > 64
        or any(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", scope) is None for scope in scopes)
    ):
        raise ValueError("Specify valid tool names for the connection")
    parsed = urlsplit(tokens.resource)
    issuer = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    callback = _LoopbackCallback(state, issuer)
    try:
        redirect = await callback.start()
        url = (
            issuer
            + "/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": tokens.client,
                    "redirect_uri": redirect,
                    "resource": tokens.resource,
                    "scope": " ".join(sorted(scopes)),
                    "state": state,
                    "code_challenge": pkce_s256(verifier),
                    "code_challenge_method": "S256",
                }
            )
        )
        async with asyncio.timeout(timeout):
            if not await asyncio.to_thread(open_browser, url):
                raise ClientAuthorizationRequired(
                    "Could not open the browser; configure a browser and retry"
                )
            code = await callback.result
        await callback.close()
        requested_at = time.time()
        response = await asyncio.to_thread(
            exchange, tokens.resource, tokens.client, redirect, code, verifier
        )
        validated = TokenReply.model_validate(response)
        if frozenset(validated.scope.split()) != scopes:
            raise ClientAuthorizationRequired(
                "Approved permissions did not match the requested tools"
            )
        await asyncio.to_thread(tokens.install, validated, requested_at=requested_at)
    except TimeoutError:
        raise ClientAuthorizationRequired(
            "Browser authorization timed out; start login again"
        ) from None
    finally:
        await callback.close()
