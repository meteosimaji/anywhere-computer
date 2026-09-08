# Optional outbound HTTPS tunnel

The HTTP/MCP service accepts any compatible owner-managed HTTPS reverse proxy.
For hosts behind NAT, the optional `cloudflared` adapter can run an existing
remote-managed Cloudflare Tunnel under the same OS user as Anywhere Computer.
It does not create a Cloudflare account, tunnel, DNS record, or paid service.
No tunnel binary is bundled or automatically installed. The three Python runtime
dependencies remain unchanged.

## Setup

1. Configure the loopback service and owner authentication using the
   [HTTP server instructions](HTTP-SERVER.md). Its resource must be the stable
   public URL, for example `https://computer.example.com/mcp`.
2. Create a dedicated remote-managed tunnel and hostname in your Cloudflare
   account. Set its origin service to `http://127.0.0.1:8768` (or your configured
   port), and `originRequest.httpHostHeader` to `127.0.0.1:8768`. Forward all paths,
   including OAuth and well-known metadata. Add an `http_status:404` catchall.
   Preserve Origin, Cookie, Authorization and MCP protocol/session headers; do
   not cache authentication or MCP responses. Do not point the route at the
   separate local agent's TCP port.
3. Install `cloudflared` separately from its official distribution. The adapter
   checks for version 2025.4.0 or newer. Compatibility tests use **2026.2.0**;
   a version-number check alone does not certify future versions.
4. In an interactive terminal, save the tunnel token with hidden input:

   ```sh
   uv run anywhere tunnel-token --state-dir /absolute/path/to/state
   ```

   Paste only at the hidden prompt. Do not put the token in a shell command,
   environment variable, config, chat, or repository. This command can replace
   a saved token after the local tunnel runner has stopped. A write is followed
   by readback; an uncertain write is never automatically repeated or rolled back.
5. Run the configured HTTP service and tunnel in two foreground terminals:

   ```sh
   uv run anywhere http-watch --state-dir /absolute/path/to/state
   uv run anywhere tunnel-run --state-dir /absolute/path/to/state
   ```

   Both commands use the same state directory. The tunnel runner does not start
   the HTTP service or configure the provider's route. Check loopback with
   `http-doctor`, then perform the complete OAuth/MCP flow through public HTTPS.
   Neither a child PID nor `tunnel_credential_sent` proves public connectivity.

## Credentials and process lifetime

The token is bound to the resolved state directory and HTTP resource in the
native OS credential store. macOS uses Keychain, Windows uses Credential Manager,
and Linux requires a usable Secret Service or KWallet backend. Plaintext fallback
is refused. Changing the state directory/resource requires separate enrollment.

For each child start, the adapter reads the token again and creates a new one-shot
pipe. Linux/macOS use a FIFO with mode 0600 inside a private temporary directory.
Windows uses a byte-mode named pipe with a protected DACL granting access to the
current user SID, rejects remote clients and allows one server instance. The
token is never written to a regular file or passed as an argument/environment
value. The child receives only the pipe path. Python does not guarantee erasure
of immutable secret strings from memory; this is not a sandbox against same-user
processes or administrators.

An explicit empty, secret-free configuration prevents loading unrelated local
cloudflared configuration. Inherited `TUNNEL_*` and `CF_TUNNEL_*` environment
options are removed. Automatic cloudflared updates are disabled, metrics bind
to a dynamically allocated loopback port, and raw provider output is suppressed
to keep it out of application logs. Wrapper events contain only child PID,
retry count, delay and exit status. Detailed provider diagnostics remain a future
feature; do not enable arbitrary raw logging as a credential troubleshooting step.

Credential handoff has a 15-second timeout. Failure stops only the owned child.
On Windows, the pipe writer waits for the consumer to read buffered bytes before
closing for EOF; cancellation interrupts its blocking calls. If writer cleanup
cannot finish, the runner fails rather than starting another handoff.

Abnormal exits have at most five restarts, with waits of 1, 2, 4, 8 and 16 seconds.
A child lifetime of at least five minutes resets this budget; lifetime is not a
health check. Exit 0 or 130 ends the runner. Ctrl+C/SIGTERM stops the owned child
with a ten-second grace period, followed by a forced stop if necessary. A lock
prevents duplicate runners, token changes or removal while this runner is alive.
Cloudflared manages its own network retry/fallback internally. The wrapper does
not detect a live but disconnected/hung child, survive its own forced termination,
or provide OS login/reboot autostart. Service installation, continuous health
monitoring, sleep/outage recovery and one-command combined startup remain work.

`tunnel-forget --state-dir ...` removes only the local OS-store token, after the
runner stops. It does **not** revoke the provider token, delete the tunnel or DNS,
or disconnect connectors elsewhere. Rotate/revoke credentials at the provider
when that is intended, and restart connectors with their new token. There is no
automatic provider-side mutation in these commands.

## Verification scope

The shared tests open real OS pipes from a separate Python process, compare
payload hashes/EOF, and exercise absent/non-reading consumers and cleanup.
Credential tests use synthetic in-memory vaults; those do not prove a native
Windows/Linux credential service is available.

CI also runs `scripts/verify_tunnel_pipe.py --download-for-test` on all three OSes.
This opt-in verifier downloads a fixed **2026.2.0** official release into a
temporary directory, verifies its SHA-256, runs its real token-file parser with
an invalid synthetic token, and removes the binary. The token must be read to
EOF and rejected as invalid. This proves compatibility of the tested binary's
pipe input, **not** account authorization, NAT traversal, TLS, OAuth, or public
MCP operation. The existing temporary HTTPS probe remains separate evidence;
a constant hostname and full authenticated lifecycle still need a live test.

Cloudflare documents `--token-file`, but does not explicitly guarantee named
pipe/FIFO input. Keep the real-binary compatibility test when updating versions.

Sources: [run parameters](https://developers.cloudflare.com/tunnel/advanced/run-parameters/),
[origin parameters](https://developers.cloudflare.com/tunnel/advanced/origin-parameters/),
[token lifecycle](https://developers.cloudflare.com/tunnel/advanced/tunnel-tokens/),
[API setup](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel-api/),
[pinned release](https://github.com/cloudflare/cloudflared/releases/tag/2026.2.0),
[Windows pipe access control](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights),
[Windows cancellation](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-cancelsynchronousio).
