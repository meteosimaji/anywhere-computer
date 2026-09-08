# Remote transport: implemented foundation and remaining integration

`remote_transport.py` uses only Python standard-library `asyncio`, `ssl`,
`hashlib`, and framing utilities. It introduces no runtime package dependency.
TLS is provided by Python's OpenSSL binding; cryptographic primitives are not
implemented by this project.

The current internal protocol is length-prefixed bytes over mutually verified
TLS, with ALPN `anywhere-agent/1`. This is **not** a ChatGPT HTTP MCP endpoint.
The client verifies the certificate chain, hostname and enrolled server
certificate fingerprint. The listener additionally requires the client
certificate fingerprint to be enrolled. Trusting a certificate authority alone
does not enroll a device. Removal of an enrollment blocks future dispatches,
including a connection waiting to deliver a complete request. It does not undo
an operation dispatched before removal.

An enrollment currently grants the owner access to the handler attached to the
listener. It is not multi-user isolation or a filesystem sandbox. The transport
passes the enrolled identity to the handler. The bridge now filters the tool catalog and enforces per-peer tool grants.
Operation identifiers are namespaced per peer, and result lookup checks the
current grant for the originating tool. Global history is not remotely exposed.
Session and filesystem isolation between different users is not provided; the
current enrollment model is explicitly one device owner with several clients.

Frames are bounded to 8 MiB. Reads/requests/handshakes/shutdown have timeouts.
Application handlers have an active-connection limit; the OS/TLS handshake
layer still needs deployment-level connection limits for public exposure.
There are no automatic retries: the caller retains its operation ID and queries
the durable agent for the outcome after an interrupted connection. Handler
implementations must preserve dispatched work across cancellation; Engine does
this using independently owned tasks and its SQLite operation ledger.

Local integration tests generate disposable certificates with the OpenSSL CLI,
then establish real loopback TLS connections. They cover a file mutation,
repeated delivery with one effect, result lookup, disconnection during an
operation, unregistered and revoked peers, a wrong server fingerprint, hostname
mismatch, invalid oversized frames, and rejected verification-disabled TLS
contexts. The test certificates are temporary test material, never enrollment
credentials, and are deleted at fixture teardown.

Still required: production credential provisioning through OS storage,
peer/device configuration, TLS CLI provisioning, outbound rendezvous for
NAT, reconnect backoff/status, resource scopes, public HTTP MCP/OAuth,
host-to-isolated-Windows tests and an actual internet path. No remote listener
is enabled by default, and computer_status continues to report remote_ready
false until those integrations exist.

References for the standard runtime APIs:
- https://docs.python.org/3/library/ssl.html
- https://docs.python.org/3/library/asyncio-stream.html

## Usable CLI path through existing SSH

Where a verified SSH host alias already exists, configure MCP with:

```sh
anywhere remote-mcp --ssh-host windows-lab
```

On the remote host install this exact release and ensure `anywhere` is on the
noninteractive SSH PATH. The remote OS credential store must be available to the
SSH login session. A locked GUI-only store will fail clearly; this command does
not export local credentials to work around that condition. No Python SSH
library is required. System OpenSSH is an optional prerequisite.

This mode streams MCP stdio over SSH, requires an already trusted host key and
noninteractive authentication, disables forwarding, and uses keepalives. It does
not set up SSH servers, open firewall ports or register host keys. A disconnect
ends the connection; reconnect by launching another MCP session. The remote
agent and terminal work persist, and operations_get retrieves known results.

The independent mTLS bridge can also construct an MCPSession from a RemoteBackend
with a provisioned SSLContext. Integration tests exercise MCP listing, writing,
lookup and grant revocation across real loopback TLS. Production provisioning
and an external network test remain outstanding. The SSH wrapper's argument,
host validation and no-retry behavior are tested; a real SSH peer has not yet
been assigned, so internet operation is not claimed.
