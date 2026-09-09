"""Password-authenticated, request-bound consent for one enrolled device owner.

The public form cannot approve itself: the owner password must verify against
OS-keyring data initialized through a trusted local setup. Pending requests are
memory-only and expire on restart. This module does not deploy a public service.
"""

import asyncio
import hashlib
import hmac
import html
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .authorization import AuthorizationStore
from .http_mcp import HTTPResult, HTTPRoute
from .owner_credentials import OwnerCredentials


@dataclass(frozen=True)
class PendingConsent:
    client: str
    redirect: str
    resource: str
    tools: frozenset[str]
    challenge: str = field(repr=False)
    state: str = field(repr=False)
    browser_hash: str = field(repr=False)
    csrf_hash: str = field(repr=False)
    expires: float
    generation: int


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fields(value: str) -> dict[str, str]:
    if len(value) > 16384 or re.search(r"%(?![A-Fa-f0-9]{2})", value):
        raise ValueError("Invalid form encoding")
    pairs = parse_qsl(
        value,
        keep_blank_values=True,
        strict_parsing=True,
        max_num_fields=12,
        encoding="utf-8",
        errors="strict",
    )
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate form fields")
    return result


class BrowserAuthorization:
    def __init__(
        self,
        store: AuthorizationStore,
        credentials: OwnerCredentials,
        *,
        device: str,
    ) -> None:
        if credentials.resource != store.resource:
            raise ValueError("Owner credentials belong to another resource")
        self.store, self.credentials, self.device = store, credentials, device
        parsed = urlsplit(store.resource)
        # Issuer comparison is exact; unlike the browser Origin, preserve the
        # resource authority's spelling and explicit port to match discovery.
        self.issuer = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port not in (None, 443):
            host += ":" + str(parsed.port)
        self.origin = urlunsplit((parsed.scheme, host, "", "", ""))
        self.pending: dict[str, PendingConsent] = {}
        self.attempts: dict[str, deque[float]] = {}
        # Acquire in the actual worker, so observer cancellation cannot release
        # a slot while password verification is still running.
        self.password_slots = threading.BoundedSemaphore(2)

    def routes(self) -> dict[str, HTTPRoute]:
        return {"/authorize": self.authorize}

    @property
    def authorization_endpoint(self) -> str:
        """Use this exact URL for colocated OAuth discovery metadata."""
        return self.origin + "/authorize"

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        }

    @staticmethod
    def _cookie_name(identity: str) -> str:
        return "__Host-anywhere-" + identity

    def _page(self, identity: str, record: PendingConsent, csrf: str, *, error: str = "") -> bytes:
        escape = html.escape
        tools = "".join(f"<li><code>{escape(tool)}</code></li>" for tool in sorted(record.tools))
        warning = (
            "端末操作を許可すると、この端末のユーザー権限でコマンドを実行できます。"
            if any(tool.startswith("terminal_") for tool in record.tools)
            else "許可したツールは、この端末のユーザーがアクセスできるデータを扱います。"
        )
        return (
            "<!doctype html><html lang=ja><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            "<title>接続の確認 — Anywhere Computer</title>"
            "<style>body{font:16px/1.65 system-ui;background:#f4f6f8;color:#172334;margin:0}"
            "main{max-width:620px;margin:40px auto;padding:32px;background:white;"
            "border:1px solid #dce2e8;border-radius:16px}h1{font-size:28px;line-height:1.3}"
            "dt{font-weight:650}dd{margin:0 0 16px;overflow-wrap:anywhere}"
            "input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #8896a5;"
            "border-radius:6px;font:inherit}button{padding:12px 18px;margin:16px 8px 0 0;"
            "border:1px solid #244a6f;border-radius:6px;background:#244a6f;"
            "color:white;font:inherit}"
            "button[name=deny]{background:white;color:#244a6f}.error{color:#a12622}"
            "small{color:#475669}@media(max-width:680px){main{margin:12px;padding:20px}}"
            "</style><main><small>Anywhere Computer</small><h1>この接続を許可しますか？</h1>"
            f"<dl><dt>クライアント ID</dt><dd>{escape(record.client)}</dd>"
            f"<dt>端末</dt><dd>{escape(self.device)}</dd>"
            f"<dt>接続先</dt><dd>{escape(record.resource)}</dd>"
            f"<dt>認可後の戻り先</dt><dd>{escape(record.redirect)}</dd></dl>"
            f"<p>許可する機能</p><ul>{tools}</ul><p>{warning}</p>"
            "<p>接続の認可は最長24時間です。要求した覚えがない場合は拒否してください。</p>"
            f"<p class=error role=alert>{escape(error)}</p>"
            "<form method=post action=/authorize>"
            f"<input type=hidden name=request_id value='{escape(identity)}'>"
            f"<input type=hidden name=csrf value='{escape(csrf)}'>"
            "<label for=password>端末所有者のパスワード</label>"
            "<input id=password type=password name=password autocomplete=current-password "
            "maxlength=1024>"
            "<button type=submit name=approve value=yes>確認して接続を許可</button>"
            "<button type=submit name=deny value=yes>拒否</button>"
            "</form><p><small>この要求は5分で期限切れになります。"
            "パスワードは接続元のクライアントには渡しません。</small></p></main></html>"
        ).encode()

    def _error(self, status: int, message: str) -> HTTPResult:
        return (
            status,
            (
                "<!doctype html><html lang=ja><meta charset=utf-8>"
                "<title>Anywhere Computer</title><p>" + html.escape(message) + "</p></html>"
            ).encode(),
            self._headers(),
        )

    def _begin(self, query: str) -> HTTPResult:
        now = time.monotonic()
        self.pending = {key: item for key, item in self.pending.items() if item.expires > now}
        self.attempts = {key: value for key, value in self.attempts.items() if key in self.pending}
        if len(self.pending) >= 64:
            return self._error(429, "接続要求が多すぎます。少し待ってからやり直してください。")
        params = _fields(query)
        required = {
            "response_type",
            "client_id",
            "redirect_uri",
            "resource",
            "scope",
            "state",
            "code_challenge",
            "code_challenge_method",
        }
        if (
            not required <= params.keys()
            or params["response_type"] != "code"
            or params["code_challenge_method"] != "S256"
        ):
            raise ValueError("Invalid authorization request")
        state = params["state"]
        if not state or len(state) > 1024 or any(ord(c) < 32 or ord(c) > 126 for c in state):
            raise ValueError("Invalid authorization state")
        redirect = params["redirect_uri"]
        if {key for key, _ in parse_qsl(urlsplit(redirect).query)} & {
            "code", "state", "error", "iss",
        }:
            raise ValueError("Callback has reserved response fields")
        tools = frozenset(params["scope"].split())
        generation = self.store.validate_consent(
            owner=self.credentials.owner,
            device=self.device,
            client=params["client_id"],
            redirect=redirect,
            resource=params["resource"],
            tools=tools,
            challenge=params["code_challenge"],
        )
        identity, browser, csrf = (
            secrets.token_hex(16),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
        )
        record = PendingConsent(
            params["client_id"],
            redirect,
            params["resource"],
            tools,
            params["code_challenge"],
            state,
            _digest(browser),
            _digest(csrf),
            now + 300,
            generation,
        )
        self.pending[identity] = record
        headers = self._headers()
        headers["Set-Cookie"] = (
            f"{self._cookie_name(identity)}={browser}; Path=/; Max-Age=300; "
            "Secure; HttpOnly; SameSite=Strict"
        )
        return 200, self._page(identity, record, csrf), headers

    def _verify_password(self, password: str) -> bool | None:
        if not self.password_slots.acquire(blocking=False):
            return None
        try:
            return self.credentials.verify(password)
        finally:
            self.password_slots.release()

    async def _decide(self, headers: dict[str, str], body: bytes) -> HTTPResult:
        if headers.get("origin") != self.origin:
            return self._error(403, "この接続要求は確認できません。接続元からやり直してください。")
        if (
            len(body) > 16384
            or headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/x-www-form-urlencoded"
        ):
            raise ValueError("Invalid consent form")
        params = _fields(body.decode("utf-8"))
        identity, csrf = params.get("request_id", ""), params.get("csrf", "")
        if re.fullmatch(r"[a-f0-9]{32}", identity) is None:
            raise ValueError("Invalid consent request ID")
        record = self.pending.get(identity)
        cookies = SimpleCookie()
        cookies.load(headers.get("cookie", ""))
        cookie = cookies.get(self._cookie_name(identity))
        if (
            record is None
            or record.expires <= time.monotonic()
            or cookie is None
            or not hmac.compare_digest(record.browser_hash, _digest(cookie.value))
            or not hmac.compare_digest(record.csrf_hash, _digest(csrf))
        ):
            return self._error(
                403, "この接続要求は無効か期限切れです。接続元からやり直してください。"
            )
        if ("approve" in params) == ("deny" in params):
            raise ValueError("Choose one consent decision")
        if "approve" in params:
            now = time.monotonic()
            attempts = self.attempts.setdefault(identity, deque())
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                return self._error(429, "確認の試行回数を超えました。1分後にやり直してください。")
            attempts.append(now)
            try:
                valid = await asyncio.to_thread(self._verify_password, params.get("password", ""))
            except Exception:
                return self._error(
                    503, "所有者の認証を利用できません。端末の資格情報ストアを確認してください。"
                )
            if valid is None:
                return self._error(
                    503, "認証処理が混み合っています。少し待ってから再試行してください。"
                )
            if not valid:
                return (
                    403,
                    self._page(identity, record, csrf, error="パスワードを確認してください。"),
                    self._headers(),
                )
        # Verification yields to other requests. Only one decision can consume this
        # exact request, and its deadline must still hold after password verification.
        if record.expires <= time.monotonic() or self.pending.pop(identity, None) is not record:
            return self._error(403, "この接続要求は既に処理済みか期限切れです。")
        self.attempts.pop(identity, None)
        result = {"state": record.state, "iss": self.issuer}
        if "deny" in params:
            result["error"] = "access_denied"
        else:
            result["code"] = self.store.approve(
                owner=self.credentials.owner,
                device=self.device,
                client=record.client,
                redirect=record.redirect,
                resource=record.resource,
                tools=record.tools,
                challenge=record.challenge,
                generation=record.generation,
            )
        target = urlsplit(record.redirect)
        query = target.query + ("&" if target.query else "") + urlencode(result)
        response_headers = self._headers()
        response_headers["Location"] = urlunsplit(
            (target.scheme, target.netloc, target.path, query, "")
        )
        response_headers["Set-Cookie"] = (
            f"{self._cookie_name(identity)}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"
        )
        return 303, None, response_headers

    async def authorize(
        self,
        method: str,
        headers: dict[str, str],
        body: bytes,
        query: str = "",
    ) -> HTTPResult:
        try:
            if method == "GET":
                return self._begin(query)
            if method == "POST" and not query:
                return await self._decide(headers, body)
            return 405, None, {"Allow": "GET, POST", "Cache-Control": "no-store"}
        except (ValueError, UnicodeError, CookieError):
            # Never redirect an invalid request to an unverified callback.
            return self._error(400, "接続要求を確認できません。接続元からやり直してください。")
