# Isolated enrollment provider acceptance

This opt-in test connects the existing device authorization client to a real
Keycloak server. It is not a public relay, account/device registry, installer,
or rendered-browser acceptance test. No production credential store is used.
The fixture user and its plainly named synthetic password are test data only.

## Provider decision (2026-09-14)

Use Keycloak as the first isolated reference provider. Its maintained server
supplies account login, the device verification page, device-code issuance and
token redemption. This avoids implementing another account/password service
inside the manager or extending the personal single-owner HTTP authorization
database into a multi-user service.

The evaluated release is Keycloak 26.7.3, commit
`6d238b6558037085cc25c915893c3d301a80243e`, under Apache-2.0. The test used
Temurin OpenJDK 25.0.4.1+1 on macOS arm64. These are optional server/test
dependencies; neither Java nor Keycloak is added to the PC agent, core Python
dependencies, or portable plugin. A deployed operator would separately own JVM,
database, TLS, backups, upgrades and availability. No hosted deployment is
selected or claimed here.

Authlib 1.8.0 (`1a86748b31a2b1940b09cf627d1b70e03d85c077`) was considered as a
Python alternative. Its RFC 8628 grant implementation provides protocol
components but requires the embedding service to supply account verification,
credential persistence and polling-limit decisions. Prefer testing the complete
provider boundary first rather than creating those additional service features
for this acceptance slice. This is not a claim that Authlib is unsuitable.

Primary references: [Keycloak device authorization](https://www.keycloak.org/securing-apps/oidc-layers),
[Keycloak 26.7.3 source](https://github.com/keycloak/keycloak/tree/26.7.3),
[Keycloak license](https://github.com/keycloak/keycloak/blob/26.7.3/LICENSE.txt),
[supported JVMs](https://www.keycloak.org/server/supported-configurations),
[Authlib grant source](https://github.com/authlib/authlib/blob/v1.8.0/authlib/oauth2/rfc8628/device_code.py).

## Prepare a disposable server

Obtain the official distributions and verify their checksums before execution.
The artifacts used in this acceptance were:

| Artifact | SHA-256 from the publisher release metadata |
| --- | --- |
| `keycloak-26.7.3.tar.gz` | `77657f30b7e90d70f727712ce1c967f430fd6a5e9f458d32d8c6df0635345f47` |
| `OpenJDK25U-jdk_aarch64_mac_hotspot_25.0.4.1_1.tar.gz` | `61979887f7506a24a57439ff99adb8b3a7fc89977d9cfe3b8984f58a981b7b9d` |

Sources: [Keycloak release](https://github.com/keycloak/keycloak/releases/tag/26.7.3)
and [Temurin release](https://github.com/adoptium/temurin25-binaries/releases/tag/jdk-25.0.4.1%2B1).
Other OS/architecture assets need their own verified digest.

Use a new extraction/state directory, not an existing server. Copy
`tests/fixtures/anywhere-fixture-device-realm.json` into the extraction's
`data/import/` directory using exactly that filename. Keycloak checks the
filename against the realm name. Imports do not overwrite an existing realm,
so changing the fixture requires fresh disposable state.

Generate a one-day test certificate in a private test directory:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=Anywhere isolated fixture' \
  -addext 'subjectAltName=IP:127.0.0.1' \
  -keyout tls.key -out tls.pem
chmod 600 tls.key
```

Set `JAVA_HOME` for this test process to the extracted JDK home. Run `bin/kc.sh`
from the fresh Keycloak extraction (use its Windows equivalent on Windows),
substituting absolute certificate paths:

```sh
bin/kc.sh start --db=dev-file --cache=local \
  --http-enabled=false --http-host=127.0.0.1 --https-port=18443 \
  --hostname=https://127.0.0.1:18443 \
  --https-certificate-file=/absolute/test/tls.pem \
  --https-certificate-key-file=/absolute/test/tls.key --import-realm
```

Verify the listener is only `127.0.0.1:18443`, using HTTPS, before testing.
Do not use `start-dev`: the observed development profile also started a local
HTTP listener despite the supplied HTTP-disable option. The `start` command's
"production" profile label does not make this H2/test-user fixture production
ready. No admin account, public listener or system trust-store change is needed.

The fixture client supports only the device flow: ordinary authorization-code
and password grants are disabled. The future same-PC browser client must be a
separate registration enforcing PKCE S256. Applying that code-flow requirement
to the device-only fixture initially returned `invalid_request` for a missing
`code_challenge_method`; the two profiles are intentionally separated rather
than removing the normal browser flow's PKCE requirement.

## Execute the actual provider flow

From a locked project environment:

```sh
uv run python scripts/verify_enrollment_provider.py \
  --issuer https://127.0.0.1:18443/realms/anywhere-fixture-device \
  --ca /absolute/test/tls.pem --check-expiry
```

The runner accepts only the named loopback fixture realm. TLS certificate and
hostname verification stay enabled. Its form driver uses the real login and
confirmation endpoints, a fresh cookie jar per case, and refuses cross-origin
forms/redirects. It never prints codes, tokens, cookies, HTML or credentials.

Checks include successful client publication after memory-vault readback,
server rejection after user denial, local cancellation without token dispatch,
and server rejection of a consumed code. `--check-expiry` additionally waits
for actual server-side code expiration (121 seconds with this fixture), then
verifies rejection; it does not fast-forward the client's clock. Omit that
flag for the short compatibility check. Exit code 0 and the JSON case receipts
are required; a process merely starting is not a pass.

Stop the disposable server after testing. Keep any desired redacted receipt,
then remove only the disposable server state and fixture keys when no longer
needed. No real-account authorization, PKCE browser enrollment, UI integration,
cross-account device registration, relay routing, remote revocation or restart
recovery is established by this test. The independent macOS/Windows native
vault checks are recorded in `MANAGED-INSTALLATION.md`.

## Observed acceptance (2026-09-14)

The verified distributions above ran locally on macOS arm64. With the exact
realm fixture and runner in this change, the command including `--check-expiry`
completed with exit code 0:

| Case | Observed result |
| --- | --- |
| Login and approve | `grant_saved`; two client requests; memory-vault readback completed |
| Login and deny | `denied`; two client requests; no saved grant |
| Cancel locally | `cancelled`; only the initial device request; no token request or saved grant |
| Redeem an already consumed code | HTTP 400 `invalid_grant` from Keycloak |
| Server-side expiration | Waited 121 real seconds; HTTP 400 `expired_token` from Keycloak |

Three non-fixture issuer inputs also failed before certificate loading or
network access. Ruff passed for the runner; the existing strict source mypy
check passed for 86 files. Full-suite CI is separate from this opt-in server
test. These observations do not establish public service or native browser UI
acceptance, and do not replace the remaining account/device association work.
