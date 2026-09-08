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
disposable text file and a 1 KiB write limit. It does not use the normal agent,
credentials, terminal tools, registered devices or existing files. The test
creates its own authorization store and random short-lived credentials. Its
disposable client pair is saved in a separate OS keyring entry and deleted during
cleanup; an unlocked native credential store is required. The runner keeps
secrets out of arguments/output/receipts, and removes its temporary state on
completion. A unit test verifies that other paths, terminal operations and
symlink targets are rejected by this particular probe.

The runner obtains a temporary URL, waits for DNS and metadata readiness, and
then performs:

1. Protected-resource metadata retrieval through the public HTTPS hostname.
2. Public HTTP code redemption with PKCE (the test creates consent internally).
3. MCP initialization and discovery of exactly the two restricted tools.
4. File creation through the actual files_write handler.
5. Automatic client refresh through the public token endpoint, OS keyring
   persistence and reopening the client, a ping using the same MCP session,
   then a new MCP session and files_read comparison. The initial client expiry
   is deliberately aged by 845 seconds; this tests renewal without claiming
   a 15-minute endurance run.
6. Device revocation followed by HTTP 401 on the already established session.
7. Tunnel shutdown, disposable OS keyring credential deletion and temporary-state cleanup.

Each HTTPS request verifies the CA chain and the original hostname. Redirects
are not followed. When the OS resolver cannot resolve the freshly issued tunnel
hostname, the test can resolve it through Cloudflare DNS over HTTPS, verifies
that returned IPs are public, and connects to one of those IPs while retaining
TLS hostname verification. This fallback is confined to the probe; it does not
change the system resolver or the product's networking configuration. The
receipt identifies which resolver was used. Only readiness GET/DNS requests are
retried; mutation/token requests are not automatically repeated.

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
Linux internet test, a ChatGPT browser connection, or an end-to-end OAuth login.
The public metadata's authorization URL is an explicitly unimplemented test
placeholder; actual browser login and consent are not claimed. The URL is
retired when the runner exits and should not be configured as a live connector.

Production work remains: stable HTTPS hosting, owner authentication and consent,
client onboarding, persistent outbound routing, reconnection/renewal, operational
limits and platform-specific live validation. `remote_ready` therefore remains
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
