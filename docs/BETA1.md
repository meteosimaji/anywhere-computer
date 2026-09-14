# Beta 1: frozen scope and release acceptance

The owner requested a feature freeze on 2026-09-14: stabilize and release the
existing capabilities as beta, rather than require every future roadmap feature
before the first beta. The earlier stages remain a roadmap, not a claim that
those features now exist. Beta is a prerelease.

## Included scope

- Files, bounded search, binary transfers, conditional writes, backups and recovery.
- Persistent pipe terminals, incremental output and explicit cleanup.
- Office text extraction and basic DOCX/XLSX creation.
- Common local Skills and selected resources without Codex.
- Direct stdio MCP and optional Codex adapters.
- Explicit routing to owner-configured SSH/HTTP computers.
- Existing authenticated HTTP setup, manual updates and retained-client recovery.

Core operations do not require Codex inference. Optional adapters still require
their own executable, authentication and permissions. Local MCP setup does not
automatically provision public HTTPS or add a connection in ChatGPT.

## Experimental or outside beta's supported scope

The Peekaboo GUI adapter is experimental. Recent TextEdit AX observations failed
even when screenshot capture worked; successful input acknowledgement alone is
not verification. Codex-owned Computer Use cannot be promised through an external
bridge. There is no bundled Windows GUI or default existing-login browser backend.

PTY/ConPTY, rendered document previews and format-preserving document editing,
managed account/pairing relay, and catalog caching remain roadmap work.
OS restart does not restore a running process's memory. Unattended sleep/reboot
recovery is not promised without a platform-specific acceptance receipt.
The optional management application remains a preview.

## Release gates

1. Candidate source, plugin version and bundled wheel agree.
2. Required macOS, Windows and Linux CI passes for the release commit.
3. Portable archives from that main commit pass build/relocation verification and
   their GitHub provenance is verified before publication.
4. Installed Mac engine and HTTP/local connectors are checked against the candidate.
5. Fresh ChatGPT 5.6 and authenticated machine acceptance record actual successes,
   failures and blocked requests separately.
6. Windows routing is tested on the actual VM; unresolved platform limitations are
   stated in release notes rather than inferred from CI.
7. Publish immutable versioned assets, checksums and accurate release notes.

The status below is updated only from completed checks. This document is not by
itself evidence that the release gates have passed.

## Candidate evidence

Candidate preparation is in progress. The preceding main commit e8815b8 passed
all five Quality jobs and local targeted regression/build checks. Those receipts
do not qualify the beta-versioned candidate or the installed clients.
