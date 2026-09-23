# Optional outbound HTTPS tunnel

The HTTP/MCP service accepts any compatible owner-managed HTTPS reverse proxy.
For hosts behind NAT, the optional `cloudflared` adapter can run an existing
remote-managed Cloudflare Tunnel under the same OS user as Anywhere Computer.
It does not create a Cloudflare account, tunnel, DNS record, or paid service.
No tunnel binary is bundled or automatically installed. The three Python runtime
dependencies remain unchanged.

## Interactive setup

Run `uv run anywhere remote-setup --state-dir /absolute/path/to/state` in an
interactive terminal. It asks for the public HTTPS `/mcp` address, owner and OAuth
client identifiers, loopback port, access level and registered callback URLs.
Empty callback input selects the native loopback callbacks; browser clients need
their exact registered HTTPS callback URLs. The address and provider route must
be provisioned separately as described below.

Access choices are `read-only`, `files` (including modifications and transfers),
and `all` (including terminal commands). The exact current remotely eligible tool
names are stored in the grant configuration; later updates do not automatically
expand the grant. Global operation history remains local-only.

Passwords and tunnel tokens use hidden prompts and native credential storage.
The token can be left blank and added later. Rerunning resumes missing steps and
preserves existing configuration, authorization and credentials. Corrupt or
unreadable credential records are errors, not permission to replace them. Setup
is staged: a saved HTTP configuration remains available if password entry is
cancelled or credential storage fails. Password changes still use `owner-change`;
intentional token replacement still uses `tunnel-token`.

The command reports local setup state and a next step. It does not provision DNS,
install the connector, start a service, or claim public reachability. Once the
route and connector are ready, use `remote-watch`, followed by
`remote-doctor --probe-public` with the same state directory.

## Individual setup commands

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
5. Run the configured HTTP service and tunnel together in one foreground terminal:

   ```sh
   uv run anywhere remote-serve --state-dir /absolute/path/to/state
   ```

   This command binds the HTTP service before launching the tunnel runner. The
   runner keeps its bounded connector restart policy; when it exits, the HTTP
   service closes too. Ctrl+C stops the connector before closing HTTP. Existing
   HTTP/watch/tunnel owners are refused; a startup race that loses the tunnel
   lock closes this command's HTTP service without taking over the other runner.
   It does not configure the provider's route. Check loopback with
   `http-doctor`, then perform the complete OAuth/MCP flow through public HTTPS.
   Neither a child PID nor `tunnel_credential_sent` proves public connectivity.

The individual `http-watch` and `tunnel-run` commands remain available for
separate supervision. `remote-serve` is a foreground lifecycle owner, not an
OS service installer: it does not restart the HTTP process after a fatal crash,
start at login, or recover a hung runtime. A supervised connector now observes
a private pipe owned by its parent and exits when that pipe closes, including
a parent crash. Secret handoff and graceful child shutdown may delay this exit.
This is cooperative child cleanup, not OS-enforced termination of a hung child. Windows virtualenv launchers may add
an intermediate process; do not infer direct parentage from the launch PID.

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
MCP operation. For the public HTTPS probe procedure and its limits, see
[Internet testing](INTERNET-TESTING.md#dedicated-constant-tunnel-mode).

Cloudflare documents `--token-file`, but does not explicitly guarantee named
pipe/FIFO input. Keep the real-binary compatibility test when updating versions.

Sources: [run parameters](https://developers.cloudflare.com/tunnel/advanced/run-parameters/),
[origin parameters](https://developers.cloudflare.com/tunnel/advanced/origin-parameters/),
[token lifecycle](https://developers.cloudflare.com/tunnel/advanced/tunnel-tokens/),
[API setup](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel-api/),
[pinned release](https://github.com/cloudflare/cloudflared/releases/tag/2026.2.0),
[Windows pipe access control](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights),
[Windows cancellation](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-cancelsynchronousio).


## Connection diagnosis

Run `uv run anywhere remote-doctor --state-dir /absolute/path/to/state` to inspect
loopback metadata and whether the optional connector executable is available.
It does not read the credential store, start processes, or modify configuration.
Add `--probe-public` to also request the configured HTTPS metadata endpoint:

```sh
uv run anywhere remote-doctor --state-dir /absolute/path/to/state --probe-public
```

The public probe uses normal certificate and hostname verification, a five-second
async timeout and the same bounded metadata parser as the loopback probe. It
sends no authorization or cookies and does not follow redirects. Results separate
local reachability, public reachability, resource mismatch and certificate failure.
An installed executable is not evidence that a connector is running; its process
state remains `unverified`. Even matching metadata at both endpoints does not
prove that an authenticated MCP operation will succeed or identify the connector
that served it. Exit status is zero only when requested metadata probes match.


## Restarting the combined service

Use `uv run anywhere remote-watch --state-dir /absolute/path/to/state` when the
foreground command should also restart a crashed HTTP runtime. It starts
`remote-serve`, retaining a separate watcher lock, and retries failures at most
five times with delays of 1, 2, 4, 8 and 16 seconds. Five minutes of stable child
uptime resets that budget. Exit 0 or 130 stops supervision; Ctrl+C/SIGTERM stop
the owned child. Initial owner, connector and lock checks fail before the retry
loop, so missing initial credentials do not repeatedly launch a service.

The watcher owns a private pipe to the HTTP child, which owns another to the
tunnel runner. EOF tells a responsive child that its owner is gone without
relying on potentially reused parent PIDs. The hidden `--watch-parent` switch is
used only by these internal launches. No heartbeat traffic or third-party
runtime library is required. A child crash may briefly leave the old connector
lock held while it exits; a new child that loses the lock fails rather than
reusing the old connector. Retries remain bounded.

This does not install a login service, restore live terminal processes after a
runtime crash, or automatically replay operations whose result is unknown.
Clients must use operation status and the existing reconnection behavior. Hung
children and OS login/logout behavior still require further lifecycle work.

The CLI lifecycle integration tests now launch the real `remote-watch →
remote-serve → tunnel-run` chain with a disposable HTTP configuration. They kill
the actual HTTP interpreter and verify replacement processes, reuse of a persisted
OAuth grant with a fresh MCP session, and duplicate-operation suppression after
a completed write. They also kill the watcher and check that the HTTP and
connector processes stop and the loopback port closes. A subprocess-scoped test
vault and a synthetic connector consuming the real secret pipe replace the OS
credential service and external provider in these tests. This is process/HTTP
integration evidence, not public-internet or native-vault qualification; the
provider probe remains a separate test.
