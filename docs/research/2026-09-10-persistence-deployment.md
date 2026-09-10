# Persistence repair and deployment — 2026-09-10

## Pre-change evidence

The attached review's two source files match git commit 7cba272 exactly.
The actual chatgpt profile had no autostart.json receipt and no matching user
LaunchAgent file. Its old watch snapshots were preserved outside the repository
before startup. No local listener was present. The bounded public probe recorded
HTTP 530 / subcode 1033. The old tunnel snapshot recorded interrupted; the first
stop trigger is unknown. This establishes missing OS recovery ownership, not a
specific Keychain, console or network crash as the original trigger.

## Implementation and checks

See ../PERSISTENCE.md for lifecycle, policy-version, stop and upgrade contracts.
The foreground finite restart behavior remains covered. No dependencies were added.
Full suite on final code: 608 passed, 5 skipped. Ruff src/tests/scripts and mypy
58 source files passed. Wheel/source build and bundled-code consistency passed.
An earlier full run overlapped a source edit and correctly rejected the stale
bundled wheel; the package was rebuilt and the final full run above passed.

Portable archive: dist/anywhere-macos-arm64-persistence-20260910.zip.
Extracted to ~/Library/Application Support/Anywhere Computer/portable/
persistence-20260910/Anywhere Computer. Relocated runtime verification: files
roundtrip and regex subprocess passed; 2254 manifest files verified.
Existing OAuth and owner credentials were retained. Installed Codex cache remains
at its earlier version; ordinary ChatGPT uses this new remote portable runtime.

## Live OS evidence

Registered label io.anywhere-computer.4b2415f2d6e7b609735d, matching receipt and plist,
policy_version=2, Python -I and pinned connector/Codex executable.
The new service runs under launchd (parent PID 1), independently of exec sessions.
A SIGKILL of only the verified top supervisor caused launchd to start a new process.
Both local and public metadata recovered and the observed runtime hash matches the
checkout and deployed package. A separate disposable KeepAlive LaunchAgent was
started and explicitly uninstalled; its native registration was confirmed absent.
No Mac reboot, logout, sleep or broad network interruption was performed.
No interrupted user tool operation was automatically resubmitted.

Recovery receipt:

```json
{
  "old_pid": 44288,
  "old_created_at": 1789027055.647518,
  "parent_pid": 1,
  "fault": "SIGKILL owned top supervisor",
  "started_at": 1789027110.963582,
  "new_pid": 44488,
  "new_parent_pid": 1,
  "new_created_at": 1789027115.969122,
  "runtime_matches": true,
  "runtime_id": "1c1b9d9233696cbc9783e05a3efd6ebd9b3860f4663b8b10131df9bee6ad3921",
  "launch_runs": "2",
  "public_metadata_recovered": true
}
```

Final migration: persistence-final-20260910 runtime, policy 2; runtime hash 3198245af99e4708f49a3a971fe842c1faa41345087d052e64fff7668c4488e6. First migration attempt rolled back; a subsequent diagnostic attempt succeeded. Initial migration failure cause was not captured. Final full tests: 608 passed / 5 skipped. Freshly extracted final archive verified (2254 manifest files); a running installation regenerates Python bytecode and is not a fresh-archive checksum target. Production Python source hash matches.

## Authenticated ChatGPT acceptance

The existing expired authorization was renewed through the normal owner consent
page, retaining the configured scope and 24-hour grant lifetime. A new ordinary
Chat conversation with Anywhere Computer explicitly selected received the user's
test question. It reported ready and successfully read the root directory list.
Production ledger verification (tool, Unix start time, state only; no payloads):

- computer_status, 1789027754.5344472, completed
- computer_status, 1789027754.6224692, completed
- directories_list, 1789027775.5211148, completed

The final independent probe confirmed registered/native_running, matching receipt,
no persistence/isolation upgrade required, and local/public metadata reachable.
An older review conversation returned "This conversation does not support developer
MCPs" before reaching the service. The new conversation succeeded after reconnect.
The static engine remote_ready=false field does not measure public connectivity;
it was not used to infer failure. GUI control is not implemented. Grant expiry
still requires reauthorization even while the OS service remains running.


## Permanent consent follow-up

At the owner's explicit request, new grants have no deadline and the one current
ChatGPT grant was migrated with http-retain-grants. Read-only counts confirmed
one permanent unrevoked grant and one permanent unused refresh token. No expired
or revoked grant was restored. Access tokens still expire after 900 seconds;
refresh rotation, replay-family revocation, PKCE and scope checks remain enabled.
The consent page now states the no-deadline policy and later revocation option.

Deployment: portable/permanent-consent-20260910/Anywhere Computer, runtime hash
c9ac663e992f72fd2cdc5044cbaadfc16a1aa827d57ae0973fd375a102e21cb7, matched source.
The first registration upgrade rolled back, and the next attempt succeeded;
the original migration failure remains undiagnosed. Final registration/running
and public/loopback metadata probes passed. No fresh ChatGPT tool call was made
in this follow-up; the earlier authenticated acceptance is recorded above.

Validation: 610 passed, 5 skipped; Ruff and mypy passed. Fresh portable manifest
(2254 files), file roundtrip and regex subprocess passed. Simulated time ten years
later verified refresh success, short access expiry and subsequent revocation.
Legacy finite expiry and active-only migration are covered. These checks do not
guarantee that ChatGPT itself will never require account login or reconnection.
