"""Opt-in Keycloak fixture acceptance; no real accounts or public relay.

Run only against tests/fixtures/anywhere-fixture-device-realm.json.
The HTML form driver tests the provider's real HTTP flow, not a rendered browser.
OS vaults are tested separately: this runner keeps synthetic grants in memory.
"""

import argparse
import http.cookiejar
import json
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from anywhere_computer.device_authorization import DeviceAuthorizationClient, EnrollmentProvider
from anywhere_computer.enrollment_credentials import EnrollmentCredentials
from anywhere_computer.enrollment_http import https_enrollment_form


class MemoryVault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[service, account] = value


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        item = dict(attrs)
        if tag == "form":
            self.current = {"action": item.get("action", ""), "inputs": []}
            self.forms.append(self.current)
        elif tag in ("input", "button") and self.current is not None and item.get("name"):
            self.current["inputs"].append(item)

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def origin(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port


def confirm_fixture(url, context, *, approve):
    expected = origin(url)

    class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if origin(newurl) != expected:
                raise ValueError("Fixture redirect changed origin")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), SameOriginRedirect(),
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )
    response = opener.open(url, timeout=10)
    submitted = []
    for _ in range(4):
        with response:
            address = response.url
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Fixture HTML exceeded limit")
        parser = Forms()
        parser.feed(raw.decode())
        if not parser.forms:
            break
        form = parser.forms[0]
        action = urllib.parse.urljoin(address, form["action"])
        if origin(action) != expected:
            raise ValueError("Fixture form changed origin")
        fields = {item["name"]: item.get("value", "") for item in form["inputs"]
                  if item.get("type") == "hidden"}
        names = {item["name"] for item in form["inputs"]}
        if "username" in names:
            fields.update(username="fixture-user", password="synthetic-fixture-only-password")
            submitted.append("login")
        elif {"accept", "cancel"}.issubset(names):
            fields["accept" if approve else "cancel"] = "Yes"
            submitted.append("approve" if approve else "deny")
        else:
            raise ValueError("Unexpected fixture form")
        response = opener.open(action, urllib.parse.urlencode(fields).encode(), timeout=10)
    else:
        response.close()
        raise ValueError("Fixture exceeded form limit")
    assert submitted == ["login", "approve" if approve else "deny"]


def verify(issuer, certificate, *, check_expiry=False):
    parsed = urllib.parse.urlsplit(issuer)
    if (parsed.scheme != "https" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.path != "/realms/anywhere-fixture-device"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Only the explicit HTTPS loopback fixture realm is supported")
    context = ssl.create_default_context(cafile=str(certificate))
    provider = EnrollmentProvider(
        issuer=issuer,
        device_authorization_endpoint=issuer + "/protocol/openid-connect/auth/device",
        token_endpoint=issuer + "/protocol/openid-connect/token",
        client_id="anywhere-enrollment", scope="device:enroll",
    )
    receipts = []
    for outcome in ("approve", "deny", "cancel"):
        calls, codes = [], []

        def wire(url, fields, *, calls=calls, codes=codes):
            calls.append("device" if url == provider.device_authorization_endpoint else "token")
            reply = https_enrollment_form(url, fields, context=context)
            if url == provider.device_authorization_endpoint and reply.status == 200:
                codes.append(reply.fields["device_code"])
            return reply

        with tempfile.TemporaryDirectory(prefix="anywhere-provider-acceptance-") as directory:
            vault = MemoryVault()
            store = EnrollmentCredentials(Path(directory), issuer=issuer,
                                          client=provider.client_id, profile=outcome, vault=vault)
            client = DeviceAuthorizationClient(provider, store, wire=wire)
            initial = client.start()
            assert initial.phase == "waiting" and initial.verification_uri_complete
            if outcome == "cancel":
                assert client.cancel().phase == "cancelled"
                assert client.poll().phase == "cancelled" and calls == ["device"]
                assert not vault.values
            else:
                confirm_fixture(initial.verification_uri_complete, context,
                                approve=outcome == "approve")
                delay = client.progress().retry_after
                assert 0 <= delay <= 30
                time.sleep(delay)
                expected = "grant_saved" if outcome == "approve" else "denied"
                result = client.poll()
                assert result.phase == expected
                assert bool(vault.values) == (outcome == "approve")
                assert client.poll() == result and calls == ["device", "token"]
                if outcome == "approve":
                    reopened = EnrollmentCredentials(
                        Path(directory), issuer=issuer, client=provider.client_id,
                        profile=outcome, vault=vault,
                    )
                    assert reopened.access_token(
                        attempt_id=initial.attempt_id, scope=provider.scope,
                    )
                    replay = https_enrollment_form(provider.token_endpoint, {
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "client_id": provider.client_id, "device_code": codes[0],
                    }, context=context)
                    assert replay.status == 400 and replay.fields.get("error") == "invalid_grant"
            receipts.append({"case": outcome, "phase": client.progress().phase,
                             "client_requests": len(calls), "passed": True})
            vault.values.clear()
            codes.clear()
    if check_expiry:
        issued = https_enrollment_form(provider.device_authorization_endpoint, {
            "client_id": provider.client_id, "scope": provider.scope,
        }, context=context)
        assert issued.status == 200
        lifetime = issued.fields["expires_in"]
        assert isinstance(lifetime, int) and 0 < lifetime <= 180
        time.sleep(lifetime + 1)
        expired = https_enrollment_form(provider.token_endpoint, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": provider.client_id, "device_code": issued.fields["device_code"],
        }, context=context)
        assert expired.status == 400 and expired.fields.get("error") == "expired_token"
        receipts.append({"case": "server_expiry", "waited_seconds": lifetime + 1,
                         "passed": True})
    return {"provider": "Keycloak", "fixture_only": True, "issuer": issuer,
            "rendered_browser_test": False, "native_vault_test": False,
            "saved_grant_reopened": True, "used_code_rejected": True, "cases": receipts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--check-expiry", action="store_true",
                        help="Also wait for actual server-side code expiration (up to 181s)")
    args = parser.parse_args()
    # Exceptions intentionally expose no response body, HTML, codes or credentials.
    try:
        receipt = verify(args.issuer, args.ca, check_expiry=args.check_expiry)
    except Exception as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
