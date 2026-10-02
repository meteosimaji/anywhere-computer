"""Request-bound consent for one enrolled device owner.

The public form cannot approve itself: the owner password or an enrolled
passkey must verify against OS-keyring data initialized through trusted local
setup. Pending requests are memory-only and expire on restart.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import qrcode
from qrcode.image.svg import SvgPathFillImage
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .authorization import AuthorizationStore
from .authorization_ui import document, result_page
from .client_tokens import ClientCredentialError, CredentialStoreUnavailable
from .http_mcp import HTTPResult, HTTPRoute
from .owner_credentials import OwnerCredentials
from .owner_passkeys import (
    OwnerPasskeys,
    PasskeyEnrollmentLimitReached,
    PasskeyNoLongerEnrolled,
    PasskeyRecord,
    credential_bytes,
)

_PASSWORD_VISIBILITY_SCRIPT = r"""(() => {
  const field = document.getElementById('password');
  const toggle = document.getElementById('password-visibility');
  const form = document.querySelector('#approval form');
  const status = document.getElementById('submit-status');
  const passkey = document.getElementById('passkey-approve');
  const approve = form.querySelector('button[name=approve]');
  const passkeyLabel = passkey ? passkey.textContent : '';
  const deadline = Date.now() + Number(form.dataset.remainingMs);
  let submitted = false;
  let pending = false;
  let expired = false;
  function say(message, state) {
    status.textContent = message;
    if (state) status.dataset.state = state; else delete status.dataset.state;
    if (state) status.scrollIntoView({block: 'nearest'});
  }
  function hide() {
    if (!field || !toggle) return;
    field.type = 'password';
    toggle.textContent = 'Show';
    toggle.setAttribute('aria-pressed', 'false');
    toggle.setAttribute('aria-label', 'Show password');
  }
  if (toggle) toggle.addEventListener('click', () => {
    if (field.type === 'text') { hide(); return; }
    field.type = 'text';
    toggle.textContent = 'Hide';
    toggle.setAttribute('aria-pressed', 'true');
    toggle.setAttribute('aria-label', 'Hide password');
  });
  form.addEventListener('submit', hide);
  form.addEventListener('submit', (event) => {
    if (submitted || expired) { event.preventDefault(); return; }
    submitted = true;
    if (passkey && pending) {
      // A decision (e.g. Deny) superseded the open prompt; its late answer is ignored.
      pending = false;
      passkey.removeAttribute('aria-disabled');
      passkey.textContent = passkeyLabel;
    }
    say('Processing your decision…', 'pending');
    // Defer disabling: the submitter's name must remain in the encoded form.
    setTimeout(() => {
      for (const button of form.querySelectorAll('button[type=submit]')) {
        button.disabled = true;
      }
    }, 0);
  });
  // The server enforces expiry; this only stops offering controls that cannot succeed.
  const expiry = setInterval(() => {
    if (submitted || Date.now() < deadline) return;
    clearInterval(expiry);
    expired = true;
    pending = false;
    for (const button of form.querySelectorAll('button[type=submit]')) button.disabled = true;
    if (passkey) {
      // A prompt left open must not keep advertising a wait that can no longer succeed.
      passkey.removeAttribute('aria-disabled');
      passkey.textContent = passkeyLabel;
    }
    say('This request expired. Start again from your client.', 'error');
  }, 1000);
  window.addEventListener('pagehide', hide);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) hide();
  });
  if (document.getElementById('auth-error')) (field || passkey)?.focus();
  if (passkey) passkey.addEventListener('click', async event => {
    if (document.getElementById('passkey-assertion').value) return;
    event.preventDefault();
    if (pending || submitted || expired) return;
    // aria-disabled, not disabled: the final requestSubmit needs this submitter's name.
    pending = true;
    passkey.setAttribute('aria-disabled', 'true');
    passkey.textContent = 'Waiting for passkey…';
    if (approve) approve.disabled = true;
    say('Waiting for your passkey. Complete the prompt from your browser, security key or ' +
      'phone. Nothing is approved until it finishes.', 'pending');
    try {
      const encoded = passkey.dataset.options;
      const options = JSON.parse(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')));
      const decode = value => Uint8Array.from(
        atob(value.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
      options.challenge = decode(options.challenge);
      options.allowCredentials = options.allowCredentials.map(item => ({...item,
        id: decode(item.id)}));
      const credential = await navigator.credentials.get({publicKey: options});
      // A late answer after Deny or expiry is dropped: it must never submit or retry.
      if (submitted || expired) return;
      const bytes = value => btoa(String.fromCharCode(...new Uint8Array(value)))
        .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
      const assertion = {id: credential.id, rawId: bytes(credential.rawId), type: credential.type,
        response: {clientDataJSON: bytes(credential.response.clientDataJSON),
          authenticatorData: bytes(credential.response.authenticatorData),
          signature: bytes(credential.response.signature),
          userHandle: credential.response.userHandle
            ? bytes(credential.response.userHandle) : null}};
      document.getElementById('passkey-assertion').value = JSON.stringify(assertion);
      say('Passkey received. Verifying…', 'pending');
      form.requestSubmit(passkey);
    } catch (error) {
      if (submitted || expired) return;
      pending = false;
      passkey.removeAttribute('aria-disabled');
      passkey.textContent = passkeyLabel;
      if (approve) approve.disabled = false;
      say(error && error.name === 'NotAllowedError'
        ? 'Passkey approval was cancelled or timed out. Nothing was approved. You can try ' +
          (field ? 'the passkey again or use the owner password.' : 'the passkey again.')
        : 'Passkey approval was cancelled or unavailable. Nothing was approved. ' +
          (field ? 'Use the owner password or try again in a browser that supports passkeys.'
            : 'Try again in a browser that supports passkeys.'), 'error');
    }
  });
})();"""
_PASSWORD_VISIBILITY_HASH = base64.b64encode(
    hashlib.sha256(_PASSWORD_VISIBILITY_SCRIPT.encode()).digest()
).decode('ascii')

_PASSKEY_REGISTRATION_SCRIPT = r"""(() => {
  const button = document.getElementById('register-passkey');
  const phoneButton = document.getElementById('register-on-phone');
  const phone = document.getElementById('phone-registration');
  const options = document.getElementById('registration-options');
  const title = document.getElementById('registration-title');
  const status = document.getElementById('registration-status');
  const buttonLabel = button.textContent;
  const device = title.dataset.device;
  let stopped = false;
  let paused = false;
  let phoneStarted = false;
  let pending = false;
  let submitted = false;
  let generation = 0;
  let timer;
  let request;
  const deadline = Date.now() + Number(button.dataset.remainingMs);
  function say(message, state) {
    status.textContent = message;
    if (state) status.dataset.state = state; else delete status.dataset.state;
    if (state) status.scrollIntoView({block: 'nearest'});
  }
  // One registration POST per page: a second click, Enter, or forced submit while the
  // server is still verifying would only produce a misleading "expired" error page.
  button.form.addEventListener('submit', event => {
    if (submitted) { event.preventDefault(); return; }
    submitted = true;
    say('Passkey received. Verifying and saving…', 'pending');
    // Defer disabling: the submitter must stay in the encoded form.
    setTimeout(() => {
      button.disabled = true;
      if (phoneButton) phoneButton.disabled = true;
    }, 0);
  });
  function finish(message, registered) {
    stopped = true;
    clearTimeout(timer);
    if (request) request.abort();
    if (phone) { phone.replaceChildren(); phone.hidden = true; }
    button.disabled = true;
    if (phoneButton) phoneButton.disabled = true;
    // Outcome view: a concrete heading and title, and none of the old QR/form/wait text.
    options.hidden = true;
    document.getElementById('registration-intro').hidden = true;
    const heading = registered ? 'Passkey registered.' : 'Registration can’t continue';
    title.textContent = heading;
    document.title = heading + ' — Anywhere Computer';
    if (registered) {
      button.form.hidden = true;
      for (const input of button.form.querySelectorAll('input')) input.value = '';
      history.replaceState(null, '', '/owner-passkey');
    }
    say(message, registered ? 'success' : 'error');
  }
  async function poll() {
    if (stopped || paused) return;
    const currentGeneration = generation;
    if (Date.now() >= deadline) {
      finish('Registration link expired. Check enrolled keys before starting again locally.',
        false);
      return;
    }
    const controller = new AbortController();
    request = controller;
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const form = button.form;
      const body = new URLSearchParams();
      for (const name of ['request_id', 'csrf', 'ticket']) {
        body.set(name, form.elements.namedItem(name).value);
      }
      const response = await fetch('/owner-passkey-status', {
        method: 'POST', body, credentials: 'same-origin', cache: 'no-store',
        signal: controller.signal,
      });
      if (stopped || paused || currentGeneration !== generation) return;
      if (response.status === 403 || response.status === 410) {
        finish('Registration link is no longer available. Check enrolled keys locally.', false);
        return;
      }
      if (!response.ok) throw new Error('Registration status unavailable');
      const result = await response.json();
      if (stopped || paused || currentGeneration !== generation) return;
      if (result.status === 'registered') {
        finish('This passkey can now approve connection requests for ' + device +
          '. You can close this page.', true);
        return;
      }
      if (result.status !== 'waiting') throw new Error('Registration status unavailable');
      say('Waiting for registration on your phone…', 'pending');
    } catch (_) {
      if (!stopped && !paused && currentGeneration === generation) say(
        'Cannot confirm registration yet. Retrying; do not register again.', 'warning');
    } finally { clearTimeout(timeout); }
    if (!stopped && !paused && currentGeneration === generation) timer = setTimeout(poll, 2000);
  }
  if (phoneButton) phoneButton.addEventListener('click', () => {
    phoneStarted = true;
    // The chosen route becomes the status line; the QR and its explanation move up.
    button.form.hidden = true;
    document.getElementById('phone-choice').hidden = true;
    phone.hidden = false;
    if (phoneButton) phoneButton.disabled = true;
    button.disabled = true;
    say('Waiting for registration on your phone…', 'pending');
    poll();
  });
  window.addEventListener('pagehide', () => {
    paused = true;
    generation += 1;
    clearTimeout(timer);
    if (request) request.abort();
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted || !paused || stopped) return;
    paused = false;
    if (phoneStarted) poll();
  });
  button.addEventListener('click', async event => {
    if (document.getElementById('registration-response').value) return;
    event.preventDefault();
    if (pending || stopped || !button.form.reportValidity()) return;
    // aria-disabled, not disabled: the final requestSubmit needs this submitter.
    pending = true;
    button.setAttribute('aria-disabled', 'true');
    button.textContent = 'Waiting for passkey…';
    if (phoneButton) phoneButton.disabled = true;
    say('Waiting for your browser’s passkey prompt. Complete it to register. Nothing is ' +
      'saved until verification finishes.', 'pending');
    try {
      const encoded = button.dataset.options;
      const creation = JSON.parse(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')));
      const decode = value => Uint8Array.from(
        atob(value.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
      creation.challenge = decode(creation.challenge);
      creation.user.id = decode(creation.user.id);
      creation.excludeCredentials = creation.excludeCredentials.map(
        item => ({...item, id: decode(item.id)}));
      const credential = await navigator.credentials.create({publicKey: creation});
      const bytes = value => btoa(String.fromCharCode(...new Uint8Array(value)))
        .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
      document.getElementById('registration-response').value = JSON.stringify({
        id: credential.id, rawId: bytes(credential.rawId), type: credential.type,
        response: {clientDataJSON: bytes(credential.response.clientDataJSON),
          attestationObject: bytes(credential.response.attestationObject)}});
      button.form.requestSubmit(button);
    } catch (_) {
      if (submitted) return;
      pending = false;
      button.removeAttribute('aria-disabled');
      button.textContent = buttonLabel;
      if (!stopped && phoneButton) phoneButton.disabled = false;
      say('Passkey registration was cancelled or unavailable. Nothing was saved. ' +
        (phoneButton ? 'You can try again on this device or use your phone.'
          : 'You can try again with Windows Hello on this device.'), 'error');
    }
  });
})();"""
_PASSKEY_REGISTRATION_HASH = base64.b64encode(
    hashlib.sha256(_PASSKEY_REGISTRATION_SCRIPT.encode()).digest()
).decode("ascii")

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
    passkey_challenge: bytes = field(repr=False)


@dataclass(frozen=True)
class PendingRegistration:
    challenge: bytes = field(repr=False)
    ticket_digest: str = field(repr=False)
    browser_hash: str = field(repr=False)
    csrf_hash: str = field(repr=False)
    expires: float
    enrolled_credential_id: str | None = field(default=None, repr=False)


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
        self.passkeys = OwnerPasskeys(credentials, device=device)
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
        self.registrations: dict[str, PendingRegistration] = {}
        self.registration_lock = threading.Lock()
        self.attempts: dict[str, deque[float]] = {}
        # Acquire in the actual worker, so observer cancellation cannot release
        # a slot while password verification is still running.
        self.password_slots = threading.BoundedSemaphore(2)

    def routes(self) -> dict[str, HTTPRoute]:
        return {"/authorize": self.authorize, "/owner-passkey": self.owner_passkey,
                "/owner-passkey-status": self.owner_passkey_status}

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
        # Summaries follow the exact requested names; the full list stays authoritative.
        if record.tools & {"files_read", "files_read_binary", "files_read_many",
                           "documents_read", "documents_preview"}:
            abilities.append("read files and documents")
        if "audio_capture" in record.tools:
            abilities.append("record system audio playback to a new directory")
        if record.tools & {"media_audio_clip", "media_video_frames"}:
            abilities.append("decode short audio clips and video frames from local files")
        if "media_transcribe" in record.tools:
            abilities.append("transcribe up to ten seconds of local audio or video")
        if record.tools & {"files_write", "files_write_binary", "files_edit", "files_restore",
                           "files_move", "directories_create", "documents_write",
                           "documents_edit_paragraph", "documents_edit_cell", "upload_commit"}:
            abilities.append("change files")
        if record.tools & {"terminal_start", "terminal_input", "mcp_session_open"}:
            abilities.append("run commands as this device user")
        if record.tools & {"gui_click", "gui_type", "gui_key", "gui_native_press",
                           "gui_native_set_value", "gui_native_press_target",
                           "gui_native_set_value_target", "gui_native_action"}:
            abilities.append("control apps")
        if record.tools & {"browser_navigate", "browser_click", "browser_fill",
                           "browser_dialog_handle", "browser_tab_close"}:
            abilities.append("interact with websites")
        if "devices_call" in record.tools:
            abilities.append("operate registered computers")
        if any(tool in {"mcp_call", "codex_plugin_call"} for tool in record.tools):
            abilities.append("call other connected services")
        if record.tools & {"subchat_send", "subchat_message"}:
            abilities.append("send Chat messages")
        if "subchat_save_file" in record.tools:
            abilities.append("save verified Chat files to a selected computer")
        if "subchat_delete" in record.tools:
            abilities.append("hide Chat conversations")
        if "settings_update" in record.tools:
            abilities.append("change shared engine settings")
        if record.tools & {"processes_stop", "terminal_stop", "mcp_session_close"}:
            abilities.append("stop processes")
        warning = (
            "<ul class=abilities>"
            + "".join(f"<li>{ability}</li>" for ability in abilities) + "</ul>"
            if abilities else
            "<p>This connection can use the requested tools listed below.</p>"
        )
        destination = urlsplit(record.redirect).hostname or record.redirect
        # A long list must not push the approval controls out of reach; it stays one
        # keyboard-focusable disclosure away, in full.
        tools_open = " open" if len(record.tools) <= 6 else ""
        remaining_ms = max(0, int((record.expires - time.monotonic()) * 1000))
        error_html = (
            f"<p id=auth-error class=alert role=alert>{escape(error)}</p>" if error else ""
        )
        indirect_notice = (
            "<p class=scope-notice role=note>Plugin and direct MCP tools can invoke "
            "other installed services, potentially including Subchat. Direct "
            "<code>subchat_*</code> scopes do not restrict those separate routes.</p>"
            if record.tools & {"codex_plugin_call", "mcp_call", "mcp_session_open"} else ""
        )
        scope_notice = (
            "<p class=scope-note role=note>Some Subchat tools available on this device were not "
            "requested by this connection. Approving this page will not grant those direct "
            "tools; the client must request the updated tools and the owner must approve a "
            "new consent page.</p>"
            if record.missing_subchat_tools else ""
        )
        passkey_only = self.credentials.is_passkey_only()
        passkey_html = ""
        separator = "" if passkey_only else "<div class=or aria-hidden=true>or</div>"
        try:
            enrolled = self.passkeys.list()
            if enrolled:
                options = generate_authentication_options(
                    rp_id=urlsplit(self.origin).hostname or "",
                    challenge=record.passkey_challenge,
                    allow_credentials=[PublicKeyCredentialDescriptor(id=credential_bytes(item)[0])
                                       for item in enrolled],
                    user_verification=UserVerificationRequirement.REQUIRED,
                )
                options_b64 = base64.urlsafe_b64encode(
                    options_to_json(options).encode()
                ).decode().rstrip("=")
                passkey_html = (
                    f"{separator}"
                    "<p class=hint id=passkey-hint>Use a saved passkey. Your browser opens the "
                    "prompt, not this page. A phone passkey may need Bluetooth near this "
                    "computer. Scanning the browser's QR code only starts the phone prompt; "
                    "nothing is approved until you finish verification there.</p>"
                    "<input id=passkey-assertion type=hidden name=assertion>"
                    "<button class='btn secondary' type=submit id=passkey-approve name=passkey "
                    f"value=yes formnovalidate aria-describedby=passkey-hint "
                    f"data-options='{options_b64}'>Allow with passkey</button>"
                )
        except (ClientCredentialError, ValueError):
            if passkey_only:
                raise  # No password fallback exists; fail closed.
        password_html = (
            "<label for=password>Anywhere Computer owner password</label>"
            "<p class=hint id=password-hint>This is not your computer or ChatGPT login "
            "password.</p>"
            "<div class=password-row>"
            "<input id=password type=password name=password autocomplete=current-password "
            f"maxlength=1024 aria-describedby='password-hint{' auth-error' if error else ''}' "
            f"{'aria-invalid=true ' if error else ''}required>"
            "<button class='btn quiet' type=button id=password-visibility "
            "aria-controls=password aria-pressed=false aria-label='Show password'>Show</button>"
            "</div>"
            "<button class=btn type=submit name=approve value=yes>Allow connection</button>"
            if not passkey_only else
            "<p class=hint>Use the owner passkey saved with Windows Hello on this computer.</p>"
        )
        owner_detail = (
            "<dt>Owner password</dt><dd>Set with <code>anywhere owner-init</code> "
            "(at least 8 characters) on the computer; it is not shared with the client.</dd>"
            if not passkey_only else
            "<dt>Owner passkey</dt><dd>Started locally with "
            "<code>anywhere owner-passkey-init</code> and verified with Windows Hello.</dd>"
        )
        recovery = (
            f"<details{' open' if error else ''}><summary>Forgot the owner password?</summary>"
            "<div class=details-body><p>Stop the HTTP service and run "
            "<code>anywhere owner-reset</code> on the computer. It revokes every grant of "
            "this device, clears all enrolled passkeys and disables the device until you "
            "re-enable it, so every client must connect again.</p></div></details>"
            if not passkey_only else
            f"<details{' open' if error else ''}><summary>Lost the owner passkey?</summary>"
            "<div class=details-body><p>Stop the HTTP service and run "
            "<code>anywhere owner-passkey-reset</code> locally. It revokes every grant "
            "and disables this device. Register a new Windows Hello passkey and "
            "re-enable the device before connecting again.</p></div></details>"
        )
        credential_notice = (
            "Your passkey remains on your device." if passkey_only else
            "Your password is not shared with the connecting client."
        )
        body = (
            "<h1>Allow this connection?</h1>"
            "<div class=consent><section class=summary>"
            f"<p>Access will be sent to <strong>{escape(destination)}</strong>.</p>"
            "<p class=muted>This is the return address registered for the client; a hostname "
            "alone does not show who operates it. Approve only if you started this "
            "connection.</p>"
            "<dl class=facts>"
            # Allow a line break after each slash so a long URL wraps at a path boundary.
            f"<dt>Return address</dt><dd>{escape(record.redirect).replace('/', '/<wbr>')}</dd>"
            f"<dt>Device</dt><dd>{escape(self.device)}</dd></dl></section>"
            "<section class=perm><h2>What this connection can do</h2>"
            f"{warning}{indirect_notice}{scope_notice}</section>"
            # The exact scope is one tap away before the decision, collapsed when long.
            f"<div class=tools><details{tools_open}><summary>Requested tools "
            f"({len(record.tools)})</summary>"
            "<div class=tools-scroll tabindex=0 role=region "
            "aria-label='Complete list of requested tools'>"
            f"<ul>{tools}</ul></div></details></div>"
            "<section class=auth id=approval aria-labelledby=approval-heading>"
            "<h2 id=approval-heading>Owner verification</h2>"
            "<p class=duration><strong>Stays authorized until you revoke it</strong> with "
            "<code>anywhere http-revoke</code>. Deny it if you did not request it.</p>"
            f"{error_html}"
            f"<form method=post action=/authorize data-remaining-ms='{remaining_ms}'>"
            f"<input type=hidden name=request_id value='{escape(identity)}'>"
            f"<input type=hidden name=csrf value='{escape(csrf)}'>"
            f"{password_html}"
            f"{passkey_html}"
            "<div class=deny><button class='btn secondary' type=submit name=deny value=yes "
            "formnovalidate>Deny</button></div>"
            "<p id=submit-status class=status role=status></p>"
            "</form><p class=fine>This request expires 5 minutes after it was opened. "
            f"{credential_notice}</p></section>"
            "<div class=more>"
            "<details><summary>Technical details</summary><div class=details-body>"
            "<dl class=tech>"
            f"<dt>Client ID</dt><dd>{escape(record.client)}</dd>"
            f"<dt>Device</dt><dd>{escape(self.device)}</dd>"
            f"<dt>Resource</dt><dd>{escape(record.resource)}</dd>"
            f"<dt>Return address</dt><dd>{escape(record.redirect)}</dd>"
            f"{owner_detail}"
            "</dl></div></details>"
            # Recovery help is permanent supporting text (open after a failed attempt),
            # not part of the short error message.
            f"{recovery}"
            "</div></div>"
        )
        return document(
            "Connect — Anywhere Computer", body, script=_PASSWORD_VISIBILITY_SCRIPT
        ).encode()

    def _error(self, status: int, message: str, *, registration: bool = False) -> HTTPResult:
        # The first sentence is the concrete result (h1); the rest of the message and one
        # next step follow once, without restating the same thing three times.
        first, _, rest = message.partition(". ")
        heading = html.escape(first.rstrip("."))
        rest = rest.strip()
        local_only = rest.startswith("Start locally")
        if status == 409 and "Reset the owner" in rest:
            rest = ""
            action = (
                "A reset is the only way to clear the registration count. Stop the HTTP "
                "service and run <code>anywhere owner-reset</code> on the computer; it "
                "revokes every grant of this device and clears all enrolled passkeys. "
                "The reset disables this device until you re-enable it; every client "
                "must connect again."
            )
        elif status == 409:
            rest = ""
            action = (
                "Run <code>anywhere owner-passkey-list</code> on the computer to see the "
                "enrolled keys and their credential IDs, then "
                "<code>anywhere owner-passkey-remove --credential-id ID</code> for one you "
                "no longer use. Then run <code>anywhere owner-passkey-enroll</code> for a "
                "new link."
            )
        elif registration:
            renewal = ("owner-passkey-init" if self.credentials.is_passkey_only()
                       else "owner-passkey-enroll")
            action = (
                "On the computer running Anywhere Computer, run "
                "<code>anywhere owner-passkey-list</code> if you are unsure whether a passkey "
                f"was saved, then <code>anywhere {renewal}</code> for a new "
                "one-use link."
            )
        elif status == 429 and not rest:
            action = "Wait a minute, then try again."
        elif status == 503 and not rest:
            action = (
                "This looks temporary. Check that Anywhere Computer is running on the "
                "computer and its credential store is available, then try again."
            )
        elif not rest:
            action = (
                "Return to the app that opened this page and start the connection again. "
                "This page cannot resume or approve the request."
            )
        else:
            action = ""
        parts = [html.escape(rest)] if rest and not (registration and local_only) else []
        parts.append(action)
        body = result_page("bad", heading, *[text for text in parts if text])
        return (
            status,
            document(f"{heading} — Anywhere Computer", body, narrow=True).encode(),
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
            secrets.token_bytes(32),
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
        if sum(name in params for name in ("approve", "deny", "passkey")) != 1:
            raise ValueError("Choose one consent decision")
        if "approve" in params or "passkey" in params:
            now = time.monotonic()
            attempts = self.attempts.setdefault(identity, deque())
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                return self._error(
                    429, "Too many authentication attempts. Try again in one minute."
                )
            attempts.append(now)
            valid: bool | None
            verified_passkey_id: str | None = None
            try:
                if "passkey" in params:
                    verified_passkey_id = await asyncio.to_thread(
                        self._verify_passkey, params.get("assertion", ""), record
                    )
                    valid = verified_passkey_id is not None
                else:
                    valid = await asyncio.to_thread(
                        self._verify_password, params.get("password", "")
                    )
            except (InvalidAuthenticationResponse, ValueError):
                valid = False
            except Exception:
                return self._error(
                    503, "Owner authentication is unavailable. Check the device credential store."
                )
            if valid is None:
                return self._error(
                    503, "Authentication is busy. Wait a moment and try again."
                )
            if not valid:
                # Short failure and retry only; recovery is permanent page help.
                retry = (
                    ("The passkey could not be verified. Try again."
                     if self.credentials.is_passkey_only() else
                     "The passkey could not be verified. Try again or use the owner password.")
                    if "passkey" in params else
                    ("Use the enrolled owner passkey to approve this request."
                     if self.credentials.is_passkey_only() else
                     "Check the Anywhere Computer owner password and try again.")
                )
                return (
                    403,
                    self._page(identity, record, csrf, error=retry),
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
            def approve() -> str:
                return self.store.approve(
                    owner=self.credentials.owner,
                    device=self.device,
                    client=record.client,
                    redirect=record.redirect,
                    resource=record.resource,
                    tools=record.tools,
                    challenge=record.challenge,
                    generation=record.generation,
                )

            if verified_passkey_id is not None:
                try:
                    with self.passkeys.authorization_guard(verified_passkey_id):
                        result["code"] = approve()
                except PasskeyNoLongerEnrolled:
                    return self._error(403, "The verified passkey is no longer enrolled.")
                except TimeoutError:
                    return self._error(503, "Passkey is busy. Start a new connection request.")
                except ClientCredentialError:
                    return self._error(503, "Passkey storage is unavailable. Try again later.")
            else:
                result["code"] = approve()
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

    def _verify_passkey(self, assertion: str, consent: PendingConsent) -> str | None:
        if len(assertion) > 12000:
            return None
        try:
            packet = json.loads(assertion)
            if not isinstance(packet, dict) or not isinstance(packet.get("rawId"), str):
                return None
            credential_id = packet["rawId"]
            def verify(record: PasskeyRecord) -> int:
                _, public_key = credential_bytes(record)
                result = verify_authentication_response(
                    credential=packet,
                    expected_challenge=consent.passkey_challenge,
                    expected_rp_id=urlsplit(self.origin).hostname or "",
                    expected_origin=self.origin,
                    credential_public_key=public_key,
                    credential_current_sign_count=0 if record.synced else record.sign_count,
                    require_user_verification=True,
                )
                if result.credential_id != base64url_to_bytes(record.credential_id):
                    raise ValueError("Passkey identity changed")
                return result.new_sign_count

            return (credential_id if self.passkeys.verify_and_update_counter(
                credential_id, verify) else None)
        except (KeyError, TypeError, ValueError, InvalidAuthenticationResponse):
            return None

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

    def _registration_page(
        self, identity: str, csrf: str, ticket: str, record: PendingRegistration,
    ) -> bytes:
        passkey_only = self.credentials.is_passkey_only()
        options = generate_registration_options(
            rp_id=urlsplit(self.origin).hostname or "",
            rp_name="Anywhere Computer",
            user_name=self.credentials.owner,
            user_id=hashlib.sha256(self.credentials.account.encode()).digest(),
            challenge=record.challenge,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=(AuthenticatorAttachment.PLATFORM
                                          if passkey_only else None),
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=credential_bytes(item)[0])
                                 for item in self.passkeys.list()],
        )
        options_b64 = base64.urlsafe_b64encode(
            options_to_json(options).encode()
        ).decode().rstrip("=")
        registration_url = self.origin + "/owner-passkey?" + urlencode({"ticket": ticket})
        qr_svg = qrcode.make(
            registration_url, image_factory=SvgPathFillImage,
        ).to_string(encoding="unicode")
        remaining_ms = max(0, int((record.expires - time.monotonic()) * 1000))
        phone_html = (
            "<div id=phone-choice><div class=or aria-hidden=true>or</div>"
            "<h2>Use a phone instead</h2>"
            "<p class=hint>Show a QR code for this one-use registration link, open it "
            "on a phone you control, and register there.</p>"
            f"<button class='btn secondary' id=register-on-phone type=button "
            f"data-remaining-ms='{remaining_ms}'>Register on your phone</button></div>"
            "<section class=phone id=phone-registration hidden><h2>Scan with your phone</h2>"
            "<p>This QR code is an ordinary web link to this registration page, not a passkey "
            "sign-in prompt. Open it with your phone camera, then "
            "register a passkey in its password manager. Only scan it with a phone you control. "
            "This page confirms the result automatically.</p>"
            "<div class=qr role=img aria-label='QR code for the one-use registration web link'>"
            f"{qr_svg}</div>"
            f"<a id=phone-registration-link href='{html.escape(registration_url)}'>"
            "Registration link</a></section>"
            if not passkey_only else ""
        )
        prompt_hint = (
            "Your browser, security key or password manager shows the passkey prompt, "
            "not this page."
            if not passkey_only else
            "Choose Windows Hello on this computer and complete its face, fingerprint "
            "or PIN prompt. This page never receives that secret."
        )
        renewal_command = "owner-passkey-init" if passkey_only else "owner-passkey-enroll"
        body = (
            f"<h1 id=registration-title data-device='{html.escape(self.device)}'>"
            "Register an owner passkey</h1>"
            "<p id=registration-intro>This passkey will approve future connection requests for "
            f"<strong>{html.escape(self.device)}</strong>. "
            "Only register a passkey you control.</p>"
            "<p id=registration-status class=status role=status></p>"
            "<div id=registration-options>"
            "<form id=local-registration method=post action=/owner-passkey>"
            f"<input type=hidden name=request_id value='{identity}'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<input type=hidden name=ticket value='{ticket}'>"
            "<input type=hidden id=registration-response name=response>"
            "<label for=passkey-label>Passkey label</label>"
            "<p class=hint id=passkey-label-hint>A name to recognize this passkey in your "
            "enrolled list.</p>"
            "<input id=passkey-label type=text name=label maxlength=80 value='Owner passkey' "
            "aria-describedby=passkey-label-hint required>"
            f"<p class=hint>{prompt_hint}</p>"
            f"<button class=btn id=register-passkey type=submit "
            f"data-remaining-ms='{remaining_ms}' data-options='{options_b64}'>"
            "Register passkey</button></form>"
            f"{phone_html}"
            "<p class=fine>This registration page lasts up to 3 minutes, and the link may "
            "expire sooner. If it expires, "
            f"run <code>anywhere {renewal_command}</code> locally for a new link; check "
            "<code>anywhere owner-passkey-list</code> first if you are unsure whether a "
            "passkey was saved.</p></div>"
        )
        return document(
            "Register owner passkey — Anywhere Computer", body, narrow=True,
            script=_PASSKEY_REGISTRATION_SCRIPT,
        ).encode()

    def _registration_begin(self, query: str) -> HTTPResult:
        params = _fields(query)
        if set(params) != {"ticket"} or not self.passkeys.ticket_valid(params["ticket"]):
            return self._error(
                403, "Registration link is invalid or expired. Start locally again.",
                registration=True,
            )
        if len(self.passkeys.list()) >= 20:
            raise PasskeyEnrollmentLimitReached("Too many passkeys; reset the owner")
        now = time.monotonic()
        self.registrations = {key: value for key, value in self.registrations.items()
                              if value.expires > now}
        if len(self.registrations) >= 16:
            return self._error(429, "Too many registrations in progress.", registration=True)
        identity = secrets.token_hex(16)
        browser, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        record = PendingRegistration(
            secrets.token_bytes(32), _digest(params["ticket"]),
            _digest(browser), _digest(csrf), now + 180,
        )
        self.registrations[identity] = record
        headers = self._headers()
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            f"sha256-{_PASSWORD_VISIBILITY_HASH}", f"sha256-{_PASSKEY_REGISTRATION_HASH}"
        ) + "; connect-src 'self'"
        # The same-origin registration POST must carry its real Origin for the
        # strict check below. no-referrer makes Chrome send Origin: null.
        headers["Referrer-Policy"] = "same-origin"
        headers["Set-Cookie"] = (
            f"{self._cookie_name('enroll-' + identity)}={browser}; Path=/; Max-Age=180; "
            "Secure; HttpOnly; SameSite=Strict"
        )
        return 200, self._registration_page(identity, csrf, params["ticket"], record), headers

    def _registration_finish(self, headers: dict[str, str], body: bytes) -> HTTPResult:
        if headers.get("origin") != self.origin:
            return self._error(403, "Registration origin is invalid.", registration=True)
        if (len(body) > 131072 or headers.get("content-type", "").split(";")[0].strip().lower()
                != "application/x-www-form-urlencoded"):
            raise ValueError("Invalid registration form")
        pairs = parse_qsl(body.decode(), strict_parsing=True, max_num_fields=6,
                          encoding="utf-8", errors="strict")
        params = dict(pairs)
        if len(params) != len(pairs) or set(params) != {
            "request_id", "csrf", "ticket", "response", "label",
        }:
            raise ValueError("Invalid registration form")
        identity = params["request_id"]
        if re.fullmatch(r"[a-f0-9]{32}", identity) is None:
            raise ValueError("Invalid registration request")
        record = self.registrations.get(identity)
        cookies = SimpleCookie()
        cookies.load(headers.get("cookie", ""))
        cookie = cookies.get(self._cookie_name("enroll-" + identity))
        if (record is None or record.expires <= time.monotonic() or cookie is None
                or not hmac.compare_digest(record.browser_hash, _digest(cookie.value))
                or not hmac.compare_digest(record.csrf_hash, _digest(params["csrf"]))
                or not hmac.compare_digest(record.ticket_digest, _digest(params["ticket"]))
                or not self.passkeys.ticket_valid(params["ticket"])):
            return self._error(
                403, "Registration expired. Start locally again.", registration=True)
        label = params["label"].strip()
        if not label or len(label) > 80 or len(label.encode()) > 160:
            raise ValueError("Invalid passkey label")
        try:
            packet = json.loads(params["response"])
            verified = verify_registration_response(
                credential=packet,
                expected_challenge=record.challenge,
                expected_rp_id=urlsplit(self.origin).hostname or "",
                expected_origin=self.origin,
                require_user_verification=True,
            )
        except (ValueError, TypeError, InvalidRegistrationResponse):
            return self._error(
                403, "Passkey registration could not be verified.", registration=True)
        encoded_id = base64.urlsafe_b64encode(verified.credential_id).decode().rstrip("=")
        encoded_key = base64.urlsafe_b64encode(verified.credential_public_key).decode().rstrip("=")
        try:
            enrolled = self.passkeys.register_with_ticket(params["ticket"], PasskeyRecord(
                credential_id=encoded_id,
                public_key=encoded_key,
                sign_count=verified.sign_count,
                synced=verified.credential_device_type.value == "multi_device",
                label=label,
            ))
        except (ClientCredentialError, CredentialStoreUnavailable):
            return self._error(
                503, "Passkey storage is unavailable. Check enrolled keys and "
                "start locally again with a new link."
            )
        if not enrolled:
            return self._error(
                403, "Registration was already processed or expired.", registration=True)
        # Other browser contexts holding this locally authorized link may be
        # displaying its QR. Publish completion only after verified vault save.
        for waiter_id, waiter in tuple(self.registrations.items()):
            if hmac.compare_digest(waiter.ticket_digest, record.ticket_digest):
                self.registrations[waiter_id] = replace(
                    waiter, enrolled_credential_id=encoded_id)
        self.registrations.pop(identity, None)
        response_headers = self._headers()
        response_headers["Referrer-Policy"] = "no-referrer"
        response_headers["Set-Cookie"] = (
            f"{self._cookie_name('enroll-' + identity)}=; Path=/; Max-Age=0; "
            "Secure; HttpOnly; SameSite=Strict"
        )
        # Reached only after verification and the confirmed vault save above.
        card = result_page(
            "good", "Passkey registered.",
            f"This passkey can now approve connection requests for "
            f"<strong>{html.escape(self.device)}</strong>. You can close this page.",
            "A computer page that showed the QR code updates after it confirms the result. "
            "If it does not, run <code>anywhere owner-passkey-list</code> on the computer "
            "to check the enrolled keys.",
        )
        return (
            200,
            document("Passkey registered — Anywhere Computer", card, narrow=True).encode(),
            response_headers,
        )

    def _registration_status(self, headers: dict[str, str], body: bytes) -> HTTPResult:
        if headers.get("origin") != self.origin:
            return self._error(403, "Registration origin is invalid.")
        if (len(body) > 4096 or headers.get("content-type", "").split(";")[0].strip().lower()
                != "application/x-www-form-urlencoded"):
            raise ValueError("Invalid registration status form")
        params = _fields(body.decode())
        if set(params) != {"request_id", "csrf", "ticket"}:
            raise ValueError("Invalid registration status form")
        identity = params["request_id"]
        record = self.registrations.get(identity)
        cookies = SimpleCookie()
        cookies.load(headers.get("cookie", ""))
        cookie = cookies.get(self._cookie_name("enroll-" + identity))
        if (re.fullmatch(r"[a-f0-9]{32}", identity) is None or record is None or cookie is None
                or not hmac.compare_digest(record.browser_hash, _digest(cookie.value))
                or not hmac.compare_digest(record.csrf_hash, _digest(params["csrf"]))
                or not hmac.compare_digest(record.ticket_digest, _digest(params["ticket"]))):
            return self._error(403, "Registration status request is invalid.")
        if record.expires <= time.monotonic():
            return self._error(410, "Registration link expired. Check enrolled keys locally.")
        if record.enrolled_credential_id is not None:
            if self.passkeys.find(record.enrolled_credential_id) is None:
                return self._error(410, "Registered passkey is no longer enrolled.")
            status = "registered"
        elif self.passkeys.ticket_valid(params["ticket"]):
            status = "waiting"
        else:
            # Consumption, a replacement ticket, reset or failed vault write
            # alone cannot establish that registration completed.
            return self._error(410, "Registration link is no longer available.")
        response_headers = self._headers()
        response_headers["Content-Type"] = "application/json"
        return 200, json.dumps({"status": status}).encode(), response_headers

    def _registration_call(self, operation: Callable[[], HTTPResult]) -> HTTPResult:
        # A poll must not see a consumed ticket between the vault commit and
        # publication of completion to other browsers. Run the lock in workers,
        # including GET, so keychain access cannot block the HTTP event loop.
        with self.registration_lock:
            return operation()

    async def owner_passkey_status(
        self, method: str, headers: dict[str, str], body: bytes, query: str = "",
    ) -> HTTPResult:
        try:
            if method == "POST" and not query:
                return await asyncio.to_thread(
                    self._registration_call, lambda: self._registration_status(headers, body))
            return 405, None, {"Allow": "POST", "Cache-Control": "no-store"}
        except ClientCredentialError:
            return self._error(503, "Passkey storage is unavailable. Check enrolled keys locally.")
        except (ValueError, UnicodeError, CookieError):
            return self._error(400, "Cannot verify registration status.")

    async def owner_passkey(
        self, method: str, headers: dict[str, str], body: bytes, query: str = "",
    ) -> HTTPResult:
        try:
            if method == "GET":
                return await asyncio.to_thread(
                    self._registration_call, lambda: self._registration_begin(query))
            if method == "POST" and not query:
                return await asyncio.to_thread(
                    self._registration_call, lambda: self._registration_finish(headers, body))
            return 405, None, {"Allow": "GET, POST", "Cache-Control": "no-store"}
        except ClientCredentialError:
            return self._error(503, "Passkey storage is unavailable. Try again later.")
        except PasskeyEnrollmentLimitReached as limit:
            # Two different limits share this exception. Only the lifetime registration
            # count (128) is cleared by an owner reset; the 20-key cap needs a key removed.
            if "registrations" in str(limit):
                message = "Passkey registration limit reached. Reset the owner locally."
            else:
                message = "Passkey limit reached. Remove an unused passkey locally."
            return self._error(409, message, registration=True)
        except (ValueError, UnicodeError, CookieError):
            return self._error(
                400, "Cannot verify registration. Start locally again.", registration=True)
