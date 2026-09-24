# Public HTTPS verification

The integration runner `scripts/verify_internet.py` exercises the current HTTP
MCP and OAuth implementation through a public HTTPS edge. The default mode uses
a temporary tunnel; an explicit option uses an enrolled, dedicated constant
tunnel. Both use an already installed `cloudflared` binary. It is an optional
external adapter, not a Python dependency or bundled component. The probe does
not install an OS service or configure provider routing.

Run it explicitly from a checkout with the development environment installed:

```sh
uv run python scripts/verify_internet.py --receipt dist/internet-verification.json
```

This command temporarily publishes an isolated test endpoint. Its engine only
exposes `files_read` and `files_write`, with handlers restricted to one specific
disposable text file and a 1 KiB write limit, plus `operations_get` for this
isolated grant's operation results. Four download tools are also exposed, with
`download_begin` restricted to a generated 17 MiB binary file in the temporary directory. It does not use the normal agent,
OAuth credentials, terminal tools, registered devices or existing files. Constant
mode uses only the explicitly selected native-keyring tunnel credential. The test
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

## Verification limits

The temporary route is retired when the runner exits and must not be configured
as a live connector. A successful run checks a public HTTPS path from the same
host as the test agent; it does not establish a second physical device, a
ChatGPT browser connection, or production hosting. Browser consent is driven by
an HTTP form driver, so visual rendering and human browser login are not tested.
Provisioning, combined startup, network outage and sleep recovery, and
platform-specific live acceptance need separate checks. Cloudflare describes
Quick Tunnels as a testing facility:
[Quick Tunnels documentation](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/).

## Dedicated constant tunnel mode

```sh
uv run python scripts/verify_internet.py --tunnel-state-dir /absolute/path/to/dedicated-probe-state --receipt dist/constant-internet-verification.json
```

This mode uses an existing dedicated provider route, its configured loopback port,
and `anywhere tunnel-run`. It creates its own restricted engine, authorization
database and short-lived owner/client credentials, as in temporary mode. It does
not run the HTTP service configured in that state directory or enroll a permanent
owner password. An already occupied loopback port fails before starting a connector.

An ordinary HTTP service profile is rejected. The state must have its development
`provisioning.json` marker with the dedicated-probe purpose and completed route
phase. Its binding must match the resolved state directory, resource, device ID,
port, native-keyring account and tunnel-token SHA-256. The fingerprint is not a
usable token. This is protection against accidental profile selection, not a
security boundary against a malicious local owner. Do not turn a production
profile into a probe by editing its marker. Development provisioning must verify
the dedicated provider tunnel identity, inactive connectors, ingress/Host rules,
DNS target and provider-token/Keychain equality before recording this binding.
The verifier checks the saved binding; it does not itself access the account API.
A general provisioning/enrollment command is still pending.

After the authenticated transfer and refresh checks, this mode checks the connector
PID, parent PID and process creation time against the child observed under its
own runner. It kills that child, waits for a different observed child and public
metadata readiness, then requires an authenticated files_read with the same MCP
session ID to succeed. If the target exits before fault injection succeeds, the
run fails instead of claiming a crash was injected. This tests a connector process
crash, not a physical network interruption, OS sleep or host reboot. The HTTP
engine remains alive throughout this fault.

Cleanup revokes the disposable grants, removes both disposable OS-store entries,
stops the owned runner/observed connector identities, and closes the adapter,
authorization store and engine. Every cleanup is attempted even if another fails;
failure classes and flags are recorded without credential values. Tests cover a
late child-identity event, refusal to kill a reused PID, rejection of an unbound or
changed profile, and credential cleanup despite connector-stop failure.

The constant DNS record, provider tunnel, development binding and tunnel token
remain after a probe run. The probe server and connector stop, so a successful
receipt does not show that a production endpoint remains online. OS autostart,
health monitoring, full browser onboarding, two physical devices and live
Windows/Linux internet paths require separate verification.
