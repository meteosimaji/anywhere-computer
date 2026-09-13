# Isolated relay development

The relay is not deployed and no manager pairing workflow uses it yet. Local
MCP and personal SSH/HTTP connections remain unchanged.

## Device ownership storage

`relay_registry.py` is a separate SQLite store, not an extension of the personal
`devices.sqlite3` registry or its owner-only authorization service. Its caller
must first validate the account's issuer, subject, enrollment scope and token
audience using the selected authorization provider. Constructing `RelayAccount`
does not authenticate anyone. The storage class is not exposed as an MCP tool,
HTTP endpoint, or arbitrary manager command.

The account key is the exact issuer and subject pair. A registration retry uses
the original enrollment ID within that account and returns the same server-issued
device ID. A different name under the same enrollment ID is a conflict. Different
enrollment IDs may use the same display name; names never select an execution
target. The unique constraint and write transaction prevent concurrent retries
from allocating two devices.

Revocation retains a tombstone. Retrying the original enrollment does not restore
it to registered state. Cross-account lookup and revocation both fail without
changing the owner's record. `registered` means only that ownership was stored;
it does not mean transport connected, authentication current, or engine ready.

Stored data includes issuer, subject, display name, device ID, enrollment ID and
revocation state. There are no passwords, bearer tokens, tool arguments or results
in this database. Records currently persist until the isolated database is
removed. A production retention/deletion policy and account removal workflow are
still required before a public service. This is not end-to-end encryption.

Five storage tests cover reopen/retry, duplicate names, both subject and issuer
separation, revocation/replay, and concurrent registration. These prove the
storage boundary only. Authenticated registration endpoints, separate PC transport
credentials, outbound connections, per-request tool authorization and result
recovery remain implementation work. The existing engine ledger remains the
intended source of execution results; this registry must not become a second
operation engine or an offline write queue.
