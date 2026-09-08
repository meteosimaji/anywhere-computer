# Temporary public HTTPS verification

The integration runner `scripts/verify_internet.py` exercises the current HTTP
MCP and OAuth code-redemption implementation through a temporary public HTTPS
edge. It uses an already installed `cloudflared` binary as an optional testing
tool. Cloudflared is not a runtime dependency, bundled component, permanent
service, or the product's production relay implementation.

Run it explicitly from a checkout with the development environment installed:

```sh
uv run python scripts/verify_internet.py --receipt dist/internet-verification.json
```

This command temporarily publishes an isolated test endpoint. Its engine only
exposes `files_read` and `files_write`, with handlers restricted to one specific
disposable text file and a 1 KiB write limit, plus `operations_get` for this
isolated grant's operation results. Four download tools are also exposed, with
`download_begin` restricted to a generated 17 MiB binary file in the temporary directory. It does not use the normal agent,
credentials, terminal tools, registered devices or existing files. The test
creates its own authorization store and random short-lived credentials. Its
disposable client pair and owner-password verifier are saved in separate OS
keyring entries and deleted during cleanup; an unlocked native credential store
is required. The owner password is generated in memory for this test. The runner keeps
secrets out of arguments/output/receipts, and removes its temporary state on
completion. A unit test verifies that other paths, terminal operations and
symlink targets are rejected by this particular probe.

The runner obtains a temporary URL, waits for DNS and metadata readiness, and
then performs:

1. Protected-resource metadata retrieval through the public HTTPS hostname.
2. Native client login: the actual client creates state/PKCE and a temporary
   loopback callback. An HTTP form driver retrieves the public consent page,
   submits the synthetic owner's password with its bound Cookie/CSRF/Origin,
   and delivers the redirect to that callback. The client redeems the code over
   public HTTPS, saves its pair and closes the local callback listener.
3. MCP initialization and discovery of exactly the seven restricted tools.
4. File creation through the actual files_write handler, dropping the received
   response inside the test client, then recovering the result with operations_get.
   The write POST count must remain one; the lost response is an injected fault.
5. Prepare the generated binary download, delete its source, and fetch all 68
   chunks through public HTTPS. After 34 chunks, close and recreate HTTPBackend
   with the same credentials and resume by transfer ID. Verify each chunk and
   the full SHA-256, then close the stored copy. Client replacement is explicit,
   not a naturally occurring connection outage.
6. Automatic client refresh through the public token endpoint, OS keyring
   persistence and reopening the client, a ping using the same MCP session,
   then explicit server-side session removal, automatic recovery after HTTP 404,
   and files_read comparison. The initial client expiry
   is deliberately aged by 845 seconds; this tests renewal without claiming
   a 15-minute endurance run.
7. Device revocation followed by HTTP 401 on the already established session
   and a rejected refresh; the tool operation is reported as not executed.
8. Tunnel shutdown, disposable client/owner OS keyring deletion and temporary-state cleanup.

Each HTTPS request verifies the CA chain and the original hostname. Redirects
are not followed. When the OS resolver cannot resolve the freshly issued tunnel
hostname, the test can resolve it through Cloudflare DNS over HTTPS, verifies
that returned IPs are public, and connects to one of those IPs while retaining
TLS hostname verification. This fallback is confined to the probe; it does not
change the system resolver or the product's networking configuration. The
receipt identifies which resolver was used. Only readiness GET/DNS requests are
retried for readiness. Lost/malformed operation responses and token refresh
requests are not retried. The product HTTP client can recover once from an
explicit pre-dispatch HTTP 401/404; this probe records its write POST count.

## Observed result and limits

The September 9, 2026 macOS run completed metadata retrieval, token exchange,
write/read across MCP sessions and rejection after device revocation through
Cloudflare's public addresses. The first attempt timed out because the OS
resolver did not resolve the new hostname even when a separate DNS query did;
the runner now waits for DNS publication and records its fallback explicitly.
See the sanitized [verification receipt](research/2026-09-09-internet-verification.json).

The requesting client and agent ran on the **same Mac**, with requests leaving
through the public HTTPS edge and returning through the outbound tunnel. This
is evidence of that internet path, not a second physical device, a Windows or
Linux internet test, or a ChatGPT browser connection. The initial receipt used
internal approval and a placeholder authorization URL. The later browser-consent
and native-login receipts below use the implemented authorization route and
actual HTTP form submission. Their browser interaction is driven programmatically;
visual rendering and a human browser login are not claimed. The URL is
retired when the runner exits and should not be configured as a live connector.

Production work remains: stable HTTPS hosting, a complete server provisioning
workflow, public client registration policy, persistent outbound routing, network outage/sleep recovery,
operational limits and platform-specific live validation. Client token renewal
and explicit expired-session recovery are now implemented. `remote_ready` therefore remains
false for the normal agent. Cloudflare describes Quick Tunnels as a testing
facility, not a production service:
[Quick Tunnels documentation](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/).

The subsequent [refresh verification receipt](research/2026-09-09-internet-refresh-verification.json)
also records public token rotation and continuing the same MCP session. Each
receipt retains its tested implementation and script hashes; older receipts
remain historical evidence and are not claims about newer revisions.

The [client credential verification receipt](research/2026-09-09-internet-client-credentials-verification.json)
records the native macOS keyring save/reopen, automatic client renewal over public
HTTPS, continued MCP session, and deletion of the disposable credential. This
probe supplies its DNS-resolved, hostname-verified HTTPS callback to the client;
the default client HTTPS transport is exercised separately against a local TLS
server. Neither test establishes a production connector or browser login.

The [native login receipt](research/2026-09-09-internet-native-login-verification.json)
uses `native_login.login` to obtain and install the credentials used by `HTTPBackend`.
It records the owner-password consent flow and closure of the dynamically assigned
loopback callback. Browser rendering remains explicitly false. The injected code
exchange transport performs public HTTPS with the DNS resolution described above;
the default code exchange transport separately has real TLS tests for hostname
verification, bounded responses, and no redirects or retries. On success, the
same probe continues through file operations, refresh, reconnect and revocation.

The [HTTP client verification receipt](research/2026-09-09-internet-http-client-verification.json)
uses `HTTPBackend` itself for tool calls. Its injected lost write response is
recovered by operation ID without replay (one write POST); automatic token renewal
and explicit HTTP-session-expiry recovery are also verified. This test does not
claim a naturally occurring network outage or a browser authorization flow.


The [public download receipt](research/2026-09-09-internet-download-verification.json)
adds a complete 17 MiB transfer and midpoint client recreation through the same
public edge. Download time includes range retrieval, client recreation and close,
not initial authorization or copy preparation. It verifies Engine operation
recording and the HTTP transport path in addition to the local copy API tests.
It retains the same single-Mac, synthetic consent-driver and temporary-route
limitations described above. The published endpoint is stopped after testing.
