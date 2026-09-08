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
passes the enrolled identity to the handler. Per-tool scopes and ownership of
sessions/results must be enforced by the future provisioning/routing layer.

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
peer/device configuration, CLI and MCP integration, outbound rendezvous for
NAT, reconnect backoff/status, capability scopes, public HTTP MCP/OAuth,
host-to-isolated-Windows tests and an actual internet path. No remote listener
is enabled by default, and computer_status continues to report remote_ready
false until those integrations exist.

References for the standard runtime APIs:
- https://docs.python.org/3/library/ssl.html
- https://docs.python.org/3/library/asyncio-stream.html
