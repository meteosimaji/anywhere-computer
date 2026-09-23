# Persistent remote service

New `autostart-install` registrations use policy version 2 and run
`remote-watch --persistent` in the current user's OS session. No root service is
created. macOS uses KeepAlive and a 60-second launch throttle; Linux uses
Restart=always/RestartSec=60; Windows uses the scheduler's bounded failure-restart
policy. These OS settings are not a claim of identical cross-platform recovery.

The existing foreground watch retains its finite retries. Persistent mode adds
an outer 60-second cooldown after child exit or retry exhaustion. Child 0/130
exits are unexpected while persistent service is requested. Stop the registration
with `autostart-uninstall`; killing the process alone intentionally allows OS
recovery. Parent-pipe cleanup remains enabled.

Selected native credential-backend initialization failures represented by
KeyringError/OSError are retried with cooldown; read unavailability also retries.
A successfully selected backend instance is reused by the startup preflight.
Unsupported backends, invalid configuration, rejected/missing credentials and
other permanent startup failures enter `blocked` and await explicit stop/repair/
restart, without creating credentials or repeatedly prompting for setup.
Native calls that hang are not given a new hard timeout by this change.

`autostart-status` separates native registration/running from public readiness.
`autostart-start` explicitly starts an owned, enabled, matching stopped job;
it does not kill a running service. Disabled jobs must be deliberately re-enabled
or re-registered; start does not silently change their enabled state.

Legacy receipts missing policy_version reconstruct version 1 byte-for-byte, so
status/uninstall preserve strict checks. `autostart-upgrade` explicitly removes
the verified registration after confirming shutdown and installs with the current
interpreter and version 2. It attempts to restore the previous receipt/registration
on a caught installation failure. Migration holds a single registration lock and
retains autostart-upgrade-previous.json on interrupted or failed recovery.
It is not a crash-atomic migration: interruption
between removal and re-registration can require recovery. Credentials and ledgers
are never recreated by migration. Do not manually rewrite plist files.

Lifecycle console output is optional and catches output I/O failures; MCP stdio
and operation-ledger contracts are unchanged. Each watch file also has at most
64 atomic event snapshots recording event, time, process creation, generation,
elapsed time, runtime hash and exit code when available. They are bounded recent
observations, not a complete audit log or proof of current liveness. Provider raw
output, tokens and passwords are excluded. Diagnostic HTTP failures expose bounded
status/subcode information, not response bodies or cookies.

## Operational limits

A registered service and reachable metadata establish only the checked process and
route at the time of observation. Verify reboot, login and sleep recovery separately.
New OAuth grants have no time deadline; existing active grants can be migrated with
`http-retain-grants`, while revoked or expired grants require fresh consent. Access
tokens expire after 15 minutes and use rotating refresh tokens. The engine's static
`remote_ready=false` field is not a live public HTTPS reachability measurement; use
`remote-doctor` and authenticated client calls for that evidence.
