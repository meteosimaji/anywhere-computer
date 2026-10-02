import asyncio
import re
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from test_authorization import REDIRECT, RESOURCE, VERIFIER, pkce_s256, redeem
from test_authorization import authority as authority
from test_client_tokens import MemoryVault

from anywhere_computer.browser_authorization import BrowserAuthorization
from anywhere_computer.owner_credentials import OwnerCredentials


@pytest.fixture
def browser(authority, tmp_path, monkeypatch):
    credentials = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    # Authentication binding tests use a deterministic verifier; the real scrypt
    # implementation and persistence are tested in test_owner_credentials.
    monkeypatch.setattr(credentials, "verify", lambda password: password == "synthetic-password")
    return BrowserAuthorization(authority, credentials, device="device")


async def begin(browser, **overrides):
    parameters = dict(
        response_type="code",
        client_id="client",
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        scope="files_read",
        state="client-state",
        code_challenge=pkce_s256(VERIFIER),
        code_challenge_method="S256",
    )
    parameters.update(overrides)
    query = urlencode(parameters)
    status, body, headers = await browser.authorize("GET", {}, b"", query)
    assert status == 200
    assert headers["Referrer-Policy"] == "same-origin"
    fields = dict(re.findall(r"name=(request_id|csrf) value='([^']+)'", body.decode()))
    request_headers = {
        "origin": "https://computer.example",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": headers["Set-Cookie"].split(";", 1)[0],
    }
    return fields, request_headers


async def decide(browser, fields, headers, **overrides):
    return await browser.authorize(
        "POST",
        headers,
        urlencode(
            {**fields, "approve": "yes", "password": "synthetic-password", **overrides}
        ).encode(),
    )


async def test_consent_issues_one_bound_code(browser, authority):
    fields, headers = await begin(browser)
    results = await asyncio.gather(
        decide(browser, fields, headers), decide(browser, fields, headers)
    )
    assert sorted(item[0] for item in results) == [303, 403]
    success = next(item for item in results if item[0] == 303)
    callback = parse_qs(urlsplit(success[2]["Location"]).query)
    assert callback["state"] == ["client-state"]
    assert callback["iss"] == ["https://computer.example"]
    token = redeem(authority, callback["code"][0])
    assert authority.verify(token.value, resource=RESOURCE).tools == frozenset({"files_read"})


async def test_consent_explains_owner_password_and_keeps_tools_reviewable(browser):
    fields, _ = await begin(browser)
    record = browser.pending[fields["request_id"]]
    page = browser._page(fields["request_id"], record, fields["csrf"]).decode()
    assert "Anywhere Computer owner password" in page
    assert "not your computer or ChatGPT login password" in page
    assert "at least 8 characters" in page
    assert "<details open><summary>Requested tools (1)</summary>" in page
    assert "<li><code>files_read</code></li>" in page
    assert page.index("<li>read files and documents</li>") < page.index("Requested tools (1)")
    assert "This connection can use the requested tools" not in page
    assert "anywhere http-revoke" in page
    assert "Access will be sent to <strong>client.example</strong>" in page
    assert "<details><summary>Technical details</summary>" in page
    assert "required>" in page and "formnovalidate>Deny" in page
    assert "button.disabled = true" in page


async def test_consent_warns_when_client_omits_available_subchat_tools(tmp_path, monkeypatch):
    from anywhere_computer.authorization import AuthorizationStore

    tools = frozenset({"files_read", "subchat_send", "subchat_recover"})
    store = AuthorizationStore(tmp_path / "auth", resource=RESOURCE, known_tools=tools)
    store.register_client("client", frozenset({REDIRECT}))
    store.enroll_device("owner", "device", tools)
    credentials = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    monkeypatch.setattr(credentials, "verify", lambda password: password == "synthetic-password")
    consent = BrowserAuthorization(store, credentials, device="device")
    try:
        query = urlencode({
            "response_type": "code", "client_id": "client", "redirect_uri": REDIRECT,
            "resource": RESOURCE, "scope": "files_read", "state": "client-state",
            "code_challenge": pkce_s256(VERIFIER), "code_challenge_method": "S256",
        })
        status, page, response_headers = await consent.authorize("GET", {}, b"", query)
        assert status == 200
        assert b"Some Subchat tools available on this device were not requested" in page
        assert b"Approving this page will not grant those direct tools" in page
        assert b"<li><code>subchat_send</code></li>" not in page
        fields = dict(re.findall(r"name=(request_id|csrf) value='([^']+)'", page.decode()))
        status, _, approved = await decide(consent, fields, {
            "origin": "https://computer.example",
            "content-type": "application/x-www-form-urlencoded",
            "cookie": response_headers["Set-Cookie"].split(";", 1)[0],
        })
        assert status == 303
        code = parse_qs(urlsplit(approved["Location"]).query)["code"][0]
        token = redeem(store, code)
        assert store.verify(token.value, resource=RESOURCE).tools == frozenset({"files_read"})

        complete_query = urlencode({
            "response_type": "code", "client_id": "client", "redirect_uri": REDIRECT,
            "resource": RESOURCE, "scope": "files_read subchat_send subchat_recover",
            "state": "client-state", "code_challenge": pkce_s256(VERIFIER),
            "code_challenge_method": "S256",
        })
        status, complete_page, _ = await consent.authorize("GET", {}, b"", complete_query)
        assert status == 200
        assert b"Some Subchat tools available on this device" not in complete_page
    finally:
        store.close()


async def test_consent_explains_indirect_plugin_route_separately_from_direct_subchat(
    tmp_path, monkeypatch,
):
    from anywhere_computer.authorization import AuthorizationStore

    tools = frozenset({"files_read", "codex_plugin_call", "mcp_session_open", "mcp_call",
                       "subchat_send"})
    store = AuthorizationStore(tmp_path / "auth", resource=RESOURCE, known_tools=tools)
    store.register_client("client", frozenset({REDIRECT}))
    store.enroll_device("owner", "device", tools)
    credentials = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    monkeypatch.setattr(credentials, "verify", lambda password: password == "synthetic-password")
    consent = BrowserAuthorization(store, credentials, device="device")
    try:
        fields, _ = await begin(consent, scope="files_read codex_plugin_call mcp_call")
        record = consent.pending[fields["request_id"]]
        page = consent._page(fields["request_id"], record, fields["csrf"]).decode()
        assert "Approving this page will not grant those direct tools" in page
        assert "Plugin and direct MCP tools can invoke" in page
        assert "Direct <code>subchat_*</code> scopes do not restrict" in page
        assert "<li><code>subchat_send</code></li>" not in page
    finally:
        store.close()


@pytest.mark.parametrize("tools, expected, absent", [
    ({"documents_edit_cell"}, ("change files",), ("control apps",)),
    ({"documents_write", "browser_fill", "devices_call", "subchat_message", "codex_plugin_call"},
     ("change files", "interact with websites", "operate registered computers",
      "call other connected services", "send Chat messages"),
     ("run commands as this device user",)),
    ({"mcp_session_open", "settings_update", "processes_stop", "subchat_delete"},
     ("run commands as this device user", "change shared engine settings", "stop processes",
      "hide Chat conversations", "Plugin and direct MCP tools can invoke"),
     ("send Chat messages", "control apps")),
    ({"terminal_output", "gui_native_observe"}, (),
     ("run commands as this device user", "control apps")),
    ({"browser_dialog_handle"}, ("interact with websites",), ("control apps",)),
    ({"browser_tab_close"}, ("interact with websites",), ("control apps",)),
    ({"gui_native_press_target"}, ("control apps",),
     ("interact with websites",)),
    ({"gui_native_action"}, ("control apps",),
     ("interact with websites",)),
])
async def test_consent_summarizes_actual_write_and_delegation_tools(
    tmp_path, monkeypatch, tools, expected, absent,
):
    from anywhere_computer.authorization import AuthorizationStore

    tools = frozenset(tools)
    store = AuthorizationStore(tmp_path / "auth", resource=RESOURCE, known_tools=tools)
    store.register_client("client", frozenset({REDIRECT}))
    store.enroll_device("owner", "device", tools)
    credentials = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    monkeypatch.setattr(credentials, "verify", lambda password: password == "synthetic-password")
    consent = BrowserAuthorization(store, credentials, device="device")
    try:
        fields, _ = await begin(consent, scope=" ".join(sorted(tools)))
        record = consent.pending[fields["request_id"]]
        page = consent._page(fields["request_id"], record, fields["csrf"]).decode()
        for ability in expected:
            assert ability in page
        for ability in absent:
            assert ability not in page
    finally:
        store.close()


@pytest.mark.parametrize("tools, expected, absent", [
    ({"files_read_many", "documents_preview"}, ("read files and documents",),
     ("record system audio", "decode short audio", "transcribe up to")),
    ({"audio_capture"}, ("record system audio playback to a new directory",),
     ("read files and documents", "decode short audio", "transcribe up to")),
    ({"media_audio_clip"}, ("decode short audio clips and video frames from local files",),
     ("read files and documents", "record system audio", "transcribe up to")),
    ({"media_video_frames", "media_transcribe", "codex_plugin_call"},
     ("decode short audio clips and video frames", "transcribe up to ten seconds",
      "Plugin and direct MCP tools can invoke"),
     ("read files and documents", "record system audio")),
    # Inspection-only tools are not summarized as read/record/transcribe capabilities.
    ({"audio_status", "media_status", "files_info"}, (),
     ("read files and documents", "record system audio", "decode short audio",
      "transcribe up to")),
])
async def test_consent_summarizes_read_audio_and_media_from_exact_tool_names(
    tmp_path, monkeypatch, tools, expected, absent,
):
    from anywhere_computer.authorization import AuthorizationStore

    tools = frozenset(tools)
    store = AuthorizationStore(tmp_path / "auth", resource=RESOURCE, known_tools=tools)
    store.register_client("client", frozenset({REDIRECT}))
    store.enroll_device("owner", "device", tools)
    credentials = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    monkeypatch.setattr(credentials, "verify", lambda password: password == "synthetic-password")
    consent = BrowserAuthorization(store, credentials, device="device")
    try:
        fields, _ = await begin(consent, scope=" ".join(sorted(tools)))
        record = consent.pending[fields["request_id"]]
        page = consent._page(fields["request_id"], record, fields["csrf"]).decode()
        for ability in expected:
            assert ability in page
        for ability in absent:
            assert ability not in page
        if not expected:
            assert "This connection can use the requested tools listed below." in page
        # The exact list is never replaced by the summary.
        for tool in tools:
            assert f"<li><code>{tool}</code></li>" in page
        assert f"Requested tools ({len(tools)})" in page
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["origin", "null_origin", "cookie", "csrf", "password"])
async def test_invalid_consent_does_not_consume_request(browser, failure):
    fields, headers = await begin(browser)
    changed_fields, changed_headers = dict(fields), dict(headers)
    overrides = {}
    if failure == "null_origin":
        changed_headers["origin"] = "null"
    elif failure in {"origin", "cookie"}:
        changed_headers[failure] = "invalid"
    elif failure == "csrf":
        changed_fields["csrf"] = "invalid"
    else:
        overrides["password"] = "wrong-secret-never-echo"
    status, body, response_headers = await decide(
        browser, changed_fields, changed_headers, **overrides
    )
    assert status == 403
    assert b"wrong-secret-never-echo" not in body
    if failure == "password":
        page = body.decode()
        alert = re.search(r"<p id=auth-error class=alert role=alert>(.*?)</p>", page).group(1)
        # The failure is short; recovery is normal supporting text that stays open.
        assert alert == "Check the Anywhere Computer owner password and try again."
        assert "owner-reset" not in alert
        assert "<details open><summary>Forgot the owner password?</summary>" in page
        assert "anywhere owner-reset</code> on the computer" in page
        assert "revokes every grant of this device" in page
        assert "clears all enrolled passkeys" in page
    assert "Location" not in response_headers
    assert (await decide(browser, fields, headers))[0] == 303


async def test_revocation_between_form_and_decision_prevents_code(browser, authority):
    fields, headers = await begin(browser)
    authority.revoke_device(owner="owner", device="device")
    status, _, response_headers = await decide(browser, fields, headers)
    assert status == 400
    assert "Location" not in response_headers


@pytest.mark.parametrize("during_password", [False, True])
async def test_reenable_rejects_old_form_even_during_password_verification(
    browser, authority, monkeypatch, during_password
):
    fields, headers = await begin(browser)
    entered, release = threading.Event(), threading.Event()

    def paused_verify(password):
        entered.set()
        release.wait()
        return True

    if during_password:
        monkeypatch.setattr(browser.credentials, "verify", paused_verify)
        pending = asyncio.create_task(decide(browser, fields, headers))
    try:
        if during_password:
            assert await asyncio.to_thread(entered.wait, 30)
            assert not pending.done()
        authority.revoke_device(owner="owner", device="device")
        assert authority.enable_device(owner="owner", device="device")
    finally:
        release.set()
    result = await pending if during_password else await decide(browser, fields, headers)
    assert result[0] == 400 and "Location" not in result[2]
    assert authority.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
    new_fields, new_headers = await begin(browser)
    assert (await decide(browser, new_fields, new_headers))[0] == 303


async def test_deny_requires_browser_binding_but_no_password(browser):
    fields, headers = await begin(browser)
    status, _, response_headers = await browser.authorize(
        "POST", headers, urlencode({**fields, "deny": "yes"}).encode()
    )
    assert status == 303
    callback = parse_qs(urlsplit(response_headers["Location"]).query)
    assert callback == {"state": ["client-state"], "error": ["access_denied"],
                        "iss": ["https://computer.example"]}
    assert (await decide(browser, fields, headers))[0] == 403


async def test_registered_redirect_cannot_inject_duplicate_issuer(browser, authority):
    redirect = REDIRECT + "?iss=https%3A%2F%2Fevil.example"
    authority.register_client("injected", frozenset({redirect}))
    status, _, headers = await browser.authorize("GET", {}, b"", urlencode({
        "response_type": "code", "client_id": "injected", "redirect_uri": redirect,
        "resource": RESOURCE, "scope": "files_read", "state": "selected",
        "code_challenge": pkce_s256(VERIFIER), "code_challenge_method": "S256",
    }))
    assert status == 400 and "Location" not in headers


def test_issuer_preserves_discovery_authority_exactly(tmp_path):
    from anywhere_computer.authorization import AuthorizationStore
    from anywhere_computer.oauth_endpoints import OAuthEndpoints

    resource = "https://Computer.Example:443/mcp"
    store = AuthorizationStore(tmp_path / "auth", resource=resource, known_tools=frozenset())
    try:
        owner = OwnerCredentials(tmp_path, resource=resource, owner="owner", vault=MemoryVault())
        consent = BrowserAuthorization(store, owner, device="device")
        metadata = OAuthEndpoints(store, authorization_endpoint=consent.authorization_endpoint)
        assert consent.origin == "https://computer.example"
        assert consent.issuer == metadata.issuer == "https://Computer.Example:443"
    finally:
        store.close()


async def test_failed_attempts_do_not_lock_another_browser(browser):
    first_fields, first_headers = await begin(browser)
    other_fields, other_headers = await begin(browser)
    for _ in range(10):
        assert (await decide(browser, first_fields, first_headers, password="wrong"))[0] == 403
    assert (await decide(browser, first_fields, first_headers))[0] == 429
    assert (await decide(browser, other_fields, other_headers))[0] == 303


async def test_busy_password_workers_do_not_queue_verifications(browser):
    fields, headers = await begin(browser)
    assert browser.password_slots.acquire(blocking=False)
    assert browser.password_slots.acquire(blocking=False)
    try:
        assert (await decide(browser, fields, headers))[0] == 503
    finally:
        browser.password_slots.release()
        browser.password_slots.release()
    assert (await decide(browser, fields, headers))[0] == 303
    assert browser.authorization_endpoint == "https://computer.example/authorize"


def test_consent_csp_allows_only_callback_path_and_pinned_script():
    import base64
    import hashlib

    from anywhere_computer.browser_authorization import _PASSWORD_VISIBILITY_SCRIPT

    headers = BrowserAuthorization._headers('https://chatgpt.com/connector_platform_oauth_redirect')
    policy = headers['Content-Security-Policy']
    assert "form-action 'self' https://chatgpt.com/connector_platform_oauth_redirect;" in policy
    digest = base64.b64encode(
        hashlib.sha256(_PASSWORD_VISIBILITY_SCRIPT.encode()).digest()
    ).decode()
    assert f"script-src 'sha256-{digest}';" in policy
    assert "script-src 'unsafe-inline'" not in policy
    assert "form-action 'self';" in BrowserAuthorization._headers()['Content-Security-Policy']
    with pytest.raises(ValueError):
        BrowserAuthorization._headers('https://host;evil.example/callback')


async def test_long_tool_list_is_collapsed_but_exact_and_pages_stay_offline(tmp_path, monkeypatch):
    from anywhere_computer.authorization import AuthorizationStore
    from anywhere_computer.browser_authorization import _PASSWORD_VISIBILITY_SCRIPT

    tools = frozenset({"files_write", "mcp_call"} | {f"tool_{index:03d}" for index in range(121)})
    store = AuthorizationStore(tmp_path / "auth", resource=RESOURCE, known_tools=tools)
    store.register_client("client", frozenset({REDIRECT}))
    store.enroll_device("owner", "device", tools)
    credentials = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    monkeypatch.setattr(credentials, "verify", lambda password: password == "synthetic-password")
    consent = BrowserAuthorization(store, credentials, device="device")
    try:
        fields, _ = await begin(consent, scope=" ".join(sorted(tools)))
        record = consent.pending[fields["request_id"]]
        page = consent._page(fields["request_id"], record, fields["csrf"]).decode()
        assert len(tools) == 123
        assert "<details><summary>Requested tools (123)</summary>" in page
        assert page.count("<li><code>") == 123
        assert page.count("Requested tools (123)") == 1
        assert "<li>change files</li>" in page and "<li>call other connected services</li>" in page
        # Scope warnings stay outside the collapsed disclosure.
        assert page.index("Plugin and direct MCP tools can invoke") < page.index(
            "Requested tools (123)")
        assert "data-remaining-ms='" in page
        # The exact scope is reachable before the decision; no repeated lead-in copy.
        assert page.index("Requested tools (123)") < page.index("id=approval")
        assert "This connection can:" not in page
        assert page.count("What this connection can do") == 1
        assert "<details><summary>Forgot the owner password?</summary>" in page
        # The persistent duration is repeated beside the decision buttons.
        assert page.index("id=approval") < page.index("<p class=duration>") < page.index("<form")
        assert "Stays authorized until you revoke it</strong> with " \
               "<code>anywhere http-revoke" in page
        # Material copy about the client must not imply trust from its hostname.
        assert "a hostname alone does not show who operates it" in page
        # Offline, CSP-pinned: no remote assets, and the only script is the hashed constant.
        assert f"<script>{_PASSWORD_VISIBILITY_SCRIPT}</script>" in page
        assert page.count("<script") == 1
        for external in ("<link", " src=", "url(", "@import", "<img", "<iframe"):
            assert external not in page
    finally:
        store.close()


@pytest.mark.parametrize("status, registration, message, heading, expected", [
    (403, False, "This connection request expired. Start again from your client.",
     "This connection request expired", "Start again from your client."),
    (429, False, "Too many authentication attempts. Try again in one minute.",
     "Too many authentication attempts", "Try again in one minute."),
    (429, False, "Synthetic &failure.", "Synthetic &amp;failure", "Wait a minute, then try again"),
    (503, False, "Synthetic <b>failure</b>.", "Synthetic &lt;b&gt;failure&lt;/b&gt;",
     "credential store is available"),
    (403, False, "Synthetic failure.", "Synthetic failure",
     "Return to the app that opened this page and start the connection again"),
    (403, True, "Registration expired. Start locally again.", "Registration expired",
     "anywhere owner-passkey-enroll"),
    (409, True, "Passkey limit reached. Remove an unused passkey locally.",
     "Passkey limit reached", "anywhere owner-passkey-remove --credential-id ID"),
    (409, True, "Passkey registration limit reached. Reset the owner locally.",
     "Passkey registration limit reached", "<code>anywhere owner-reset</code>"),
])
def test_error_pages_state_the_result_once_with_one_next_step(
    browser, status, registration, message, heading, expected,
):
    code, body, headers = browser._error(status, message, registration=registration)
    page = body.decode()
    content = page.split("<main", 1)[1]
    assert code == status
    # One concrete h1 (its text is not repeated in the body) and a single next step.
    assert f"<h1>{heading}</h1>" in content and content.count(heading) == 1
    assert expected in content
    assert content.count("<h2") == 0 and content.count("<p>") <= 2
    assert "<b>failure</b>" not in page
    for suggestion in ("resend", "skip", "bypass", "disable authentication"):
        assert suggestion not in content.lower()
    if status == 409 and "owner-reset" in content:
        assert "revokes every grant" in content and "clears all enrolled passkeys" in content
        assert "disables this device until you re-enable it" in content
        assert "every client must connect again" in content
    assert headers["Content-Security-Policy"].startswith("default-src 'none'; style-src")
    assert "<script" not in page and "<link" not in page
