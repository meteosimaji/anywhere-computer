# One runtime, multiple platforms

Anywhere Computer uses one Python package for Linux, Windows and macOS. Models,
tool registry, operation ledger, file/search engine, local transport and MCP
connection are shared. The production MCP adapters use the standard library; the official MCP SDK is
a development-only interoperability test dependency.

```mermaid
flowchart LR
  Host[MCP host] --> Connector[stdio connector]
  Connector -->|Authenticated loopback| Agent[Persistent agent]
  Agent --> Ledger[SQLite operation ledger]
  Agent --> Files[Files and searches]
  Agent --> Sessions[Terminal sessions]
```

The connector is disposable. Its disconnection does not stop agent-owned terminal
sessions. A dead agent can start again on the next connection or tool call. A
live but unresponsive process is reported as unhealthy, never killed blindly.

An operation ID is claimed before execution. Identical retries reuse its result;
different arguments are rejected. After restart, unfinished operations become
`unknown`. This is not an exactly-once guarantee for arbitrary external effects.
Lost responses are never automatically replayed as writes.

## Portability

| Concern | Shared implementation | Small platform difference |
|---|---|---|
| Transport | Authenticated loopback TCP and standard MCP stdio | None |
| Credentials | keyring | Keychain / Windows Credential Manager / Secret Service or KWallet |
| State | Standard-library path selection and SQLite | Native per-user path |
| Files | pathlib, hashes, locks, atomic replacement | Exclusive create requires hard-link support |
| Terminals | asyncio session manager | Shell flags and process-tree termination |
| Validation | Same pytest suite | Three OS runners |

Headless Linux needs a usable OS credential service for detached agent mode.
Plaintext credential storage is not a fallback. Missing credentials are a setup
problem, distinct from network reachability.

## Current limits

Authenticated TLS and HTTP MCP adapters, OAuth grant/rotation endpoints and an
OS-keyring client renewal manager are implemented internally. A temporary public
HTTPS probe verifies a restricted file roundtrip and credential renewal on macOS.
Production routing, browser login/consent, device pairing, graphical UI, screen and
browser operations, service installation and standalone installers remain planned.
The normal local agent therefore still advertises remote readiness as unavailable.
Its local endpoint is not a public MCP HTTP endpoint. Office support currently
reads bounded Word, Excel and PowerPoint content; editing/rendering remain planned.

Terminals use pipes, not a PTY. Sessions survive connector disconnections but not
agent crashes. Operation records remain available after a restart.

Text reads/writes are limited to 16 MiB. Search is literal and bounded. File moves
support regular files on the same filesystem. Hashes/locks protect cooperating
writers; a last-moment external edit can still race replacement. Backups preserve
prior content, and hash-checked restore is implemented. Retention controls remain planned. Operation results
may include private content and are stored locally; diagnostic history excludes
arguments and results.
