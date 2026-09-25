"""Password-authenticated, request-bound consent for one enrolled device owner.

The public form cannot approve itself: the owner password must verify against
OS-keyring data initialized through a trusted local setup. Pending requests are
memory-only and expire on restart. This module does not deploy a public service.
"""

import asyncio
import base64
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
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .authorization import AuthorizationStore
from .http_mcp import HTTPResult, HTTPRoute
from .owner_credentials import OwnerCredentials

_PASSWORD_VISIBILITY_SCRIPT = """(() => {
  const field = document.getElementById('password');
  const toggle = document.getElementById('password-visibility');
  function hide() {
    field.type = 'password';
    toggle.textContent = 'Show';
    toggle.setAttribute('aria-pressed', 'false');
    toggle.setAttribute('aria-label', 'Show password');
  }
  toggle.addEventListener('click', () => {
    if (field.type === 'text') { hide(); return; }
    field.type = 'text';
    toggle.textContent = 'Hide';
    toggle.setAttribute('aria-pressed', 'true');
    toggle.setAttribute('aria-label', 'Hide password');
  });
  field.form.addEventListener('submit', hide);
  let submitted = false;
  field.form.addEventListener('submit', (event) => {
    if (submitted) { event.preventDefault(); return; }
    submitted = true;
    const status = document.getElementById('submit-status');
    status.textContent = 'Processing your decision…';
    // Defer disabling: the submitter's name must remain in the encoded form.
    setTimeout(() => {
      for (const button of field.form.querySelectorAll('button[type=submit]')) {
        button.disabled = true;
      }
    }, 0);
  });
  window.addEventListener('pagehide', hide);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) hide();
  });
})();"""
_PASSWORD_VISIBILITY_HASH = base64.b64encode(
    hashlib.sha256(_PASSWORD_VISIBILITY_SCRIPT.encode()).digest()
).decode('ascii')


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
    missing_subchat_tools: bool


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
    def _headers(redirect: str | None = None) -> dict[str, str]:
        form_targets = "'self'"
        if redirect is not None:
            # Called only with a registered, validated callback. CSP also checks
            # the 303 destination in Chromium; allow that exact callback path.
            target = urlsplit(redirect)
            if re.fullmatch(r"[A-Za-z0-9.\[\]:-]+", target.netloc) is None:
                raise ValueError("Callback authority cannot be represented in CSP")
            callback = urlunsplit((
                target.scheme, target.netloc, quote(target.path, safe="/%"), "", ""
            ))
            form_targets += " " + callback
        return {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            # HTML form POSTs under no-referrer send Origin: null, which our
            # strict origin check must reject. Preserve same-origin submissions
            # without disclosing the consent URL to the external callback.
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
            f"script-src 'sha256-{_PASSWORD_VISIBILITY_HASH}'; "
            f"form-action {form_targets}; base-uri 'none'; frame-ancestors 'none'",
        }

    @staticmethod
    def _cookie_name(identity: str) -> str:
        return "__Host-anywhere-" + identity

    def _page(self, identity: str, record: PendingConsent, csrf: str, *, error: str = "") -> bytes:
        escape = html.escape
        tools = "".join(f"<li><code>{escape(tool)}</code></li>" for tool in sorted(record.tools))
        abilities = []
        if "files_write" in record.tools:
            abilities.append("change files")
        if any(tool.startswith("terminal_") for tool in record.tools):
            abilities.append("run commands as this device user")
        if any(tool.startswith("gui_") for tool in record.tools):
            abilities.append("control apps")
        if any(tool in {"mcp_call", "codex_plugin_call"} for tool in record.tools):
            abilities.append("call other connected services")
        if "subchat_send" in record.tools:
            abilities.append("send Chat messages")
        warning = (
            "This connection can " + ", ".join(abilities) + "."
            if abilities else "This connection can read data available to this device user."
        )
        destination = urlsplit(record.redirect).hostname or record.redirect
        error_html = f"<p id=auth-error class=error role=alert>{escape(error)}</p>" if error else ""
        scope_notice = (
            "<p class=scope-notice role=note>Some Subchat tools available on this device were not "
            "requested by this connection. Approving this page will not grant "
            "them. To use Subchat, update or recreate the client connection "
            "with the intended Subchat tools, then review a new consent page.</p>"
            if record.missing_subchat_tools else ""
        )
        return (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            "<title>Connect — Anywhere Computer</title>"
            "<style>body{font:16px/1.65 system-ui;background:#f4f6f8;color:#172334;margin:0}"
            "main{max-width:620px;margin:40px auto;padding:32px;background:white;"
            "border:1px solid #dce2e8;border-radius:16px}h1{font-size:28px;line-height:1.3}"
            "dt{font-weight:650}dd{margin:0 0 16px;overflow-wrap:anywhere}"
            "input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #8896a5;"
            "border-radius:6px;font:inherit}button{padding:12px 18px;margin:16px 8px 0 0;"
            "border:1px solid #244a6f;border-radius:6px;background:#244a6f;"
            "color:white;font:inherit}"
            "button[name=deny]{background:white;color:#244a6f}.error{color:#a12622}"
            "summary{cursor:pointer;font-weight:600}details{margin:16px 0}"
            "details ul{padding-right:8px}"
            ".password-row{display:flex;gap:8px;align-items:center}"
            ".password-row input{min-width:0;flex:1}"
            ".password-row button{margin:0;flex:none;background:white;color:#244a6f}"
            "small{color:#475669}.scope-notice{padding:12px;border-left:4px solid #9b5800;"
            "background:#fff5e5}@media(max-width:680px){main{margin:12px;padding:20px}}"
            "</style><main><small>Anywhere Computer</small><h1>Allow this connection?</h1>"
            f"<p>Access will be sent to <strong>{escape(destination)}</strong>.</p>"
            f"<h2>What this connection can do</h2><p>{warning}</p>{scope_notice}"
            f"<details open><summary>Requested tools ({len(record.tools)})</summary>"
            f"<ul>{tools}</ul></details>"
            "<details><summary>Technical details</summary><dl>"
            f"<dt>Client ID</dt><dd>{escape(record.client)}</dd>"
            f"<dt>Device</dt><dd>{escape(self.device)}</dd>"
            f"<dt>Resource</dt><dd>{escape(record.resource)}</dd>"
            f"<dt>Return address</dt><dd>{escape(record.redirect)}</dd></dl></details>"
            "<p>This connection remains authorized until revoked with "
            "<code>anywhere http-revoke</code>. "
            "Deny it if you did not request it.</p>"
            f"{error_html}"
            "<form method=post action=/authorize>"
            f"<input type=hidden name=request_id value='{escape(identity)}'>"
            f"<input type=hidden name=csrf value='{escape(csrf)}'>"
            "<label for=password>Anywhere Computer owner password</label>"
            "<p id=password-hint><small>Set with <code>anywhere owner-init</code> "
            "(at least 8 characters); "
            "this is not your computer or ChatGPT login password.</small></p>"
            "<div class=password-row>"
            "<input id=password type=password name=password autocomplete=current-password "
            f"maxlength=1024 aria-describedby='password-hint{' auth-error' if error else ''}' "
            f"{'aria-invalid=true ' if error else ''}required>"
            "<button type=button id=password-visibility aria-controls=password "
            "aria-pressed=false aria-label='Show password'>Show</button></div>"
            "<button type=submit name=approve value=yes>Allow connection</button>"
            "<button type=submit name=deny value=yes formnovalidate>Deny</button>"
            "<p id=submit-status role=status></p>"
            "</form><p><small>This request expires 5 minutes after it was opened. "
            "Your password is not shared with the connecting client.</small></p></main>"
            f"<script>{_PASSWORD_VISIBILITY_SCRIPT}</script></html>"
        ).encode()

    def _error(self, status: int, message: str) -> HTTPResult:
        return (
            status,
            (
                "<!doctype html><html lang=en><meta charset=utf-8>"
                "<title>Anywhere Computer</title><p>" + html.escape(message) + "</p></html>"
            ).encode(),
            self._headers(),
        )

    def _begin(self, query: str) -> HTTPResult:
        now = time.monotonic()
        self.pending = {key: item for key, item in self.pending.items() if item.expires > now}
        self.attempts = {key: value for key, value in self.attempts.items() if key in self.pending}
        if len(self.pending) >= 64:
            return self._error(429, "Too many connection requests. Wait a moment and try again.")
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
        missing_subchat_tools = any(
            tool.startswith("subchat_")
            for tool in self.store.enrolled_tools(
                owner=self.credentials.owner, device=self.device
            ) - tools
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
            missing_subchat_tools,
        )
        self.pending[identity] = record
        headers = self._headers(record.redirect)
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
            return self._error(
                403, "Cannot verify this connection request. Start again from your client."
            )
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
        if record is None or record.expires <= time.monotonic():
            return self._error(
                403, "This connection request expired. Start again from your client."
            )
        if cookie is None:
            return self._error(
                403, "The connection cookie is missing. "
                "Start again from your client using the same browser."
            )
        if (
            not hmac.compare_digest(record.browser_hash, _digest(cookie.value))
            or not hmac.compare_digest(record.csrf_hash, _digest(csrf))
        ):
            return self._error(
                403, "This connection request is invalid or expired. Start again from your client."
            )
        if ("approve" in params) == ("deny" in params):
            raise ValueError("Choose one consent decision")
        if "approve" in params:
            now = time.monotonic()
            attempts = self.attempts.setdefault(identity, deque())
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                return self._error(
                    429, "Too many authentication attempts. Try again in one minute."
                )
            attempts.append(now)
            try:
                valid = await asyncio.to_thread(self._verify_password, params.get("password", ""))
            except Exception:
                return self._error(
                    503, "Owner authentication is unavailable. Check the device credential store."
                )
            if valid is None:
                return self._error(
                    503, "Authentication is busy. Wait a moment and try again."
                )
            if not valid:
                return (
                    403,
                    self._page(identity, record, csrf, error=(
                        "Check the Anywhere Computer owner password and try again. "
                        "If you forgot it, stop the HTTP service and run anywhere owner-reset "
                        "locally. That reset disconnects every client of this device."
                    )),
                    self._headers(record.redirect),
                )
        # Verification yields to other requests. Only one decision can consume this
        # exact request, and its deadline must still hold after password verification.
        if record.expires <= time.monotonic() or self.pending.pop(identity, None) is not record:
            return self._error(403, "This connection request was already processed or has expired.")
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
        response_headers = self._headers(record.redirect)
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
            return self._error(
                400, "Cannot verify the connection request. Start again from your client."
            )
