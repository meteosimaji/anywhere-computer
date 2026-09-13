# Check an existing device after reconnecting

Run `scripts/verify_device_continuity.py` with the checkout's locked Python
environment. It uses an existing device registration and its normal SSH/HTTP
backend. It does not create remote files, start terminals, restart the VM, or
change permissions. Local routing records are added as for normal device calls.

```sh
uv run python scripts/verify_device_continuity.py \
  --device-directory '/absolute/path/to/existing/state' \
  --device DEVICE_ID \
  --path 'ABSOLUTE_REMOTE_TEST_FILE' \
  --sha256 EXPECTED_SHA256 \
  --operation-id PREVIOUS_COMPLETED_OPERATION_ID
```

Use a dedicated test file and an operation created through this owner's direct
routing connection. IDs from a separately scoped ChatGPT HTTP grant are not
interchangeable with direct-owner IDs. Recovery must remain within the original
authorization namespace; this script does not translate another grant's IDs.

The JSON report includes current OS, instance and runtime identity, file-hash
comparison, saved-operation recovery, and elapsed time per call. Output omits
file contents and saved operation contents. Exit 0 means these three checks
passed; exit 1 means a failed or unconfirmed check. Compare identities with the
pre-reconnect receipt; a new instance after an agent restart is expected, while a
changed runtime must correspond to an intended update.

This is not a VM lifecycle controller. Run it before and after a separately
coordinated restart. A pass without an actual VM restart proves only current
connectivity and data continuity. It does not prove automatic VM recovery, GUI,
HVCI, EAC, or VRChat operation.

On the current development Windows-for-Mac VM, SSH still uses a loopback
forwarding rule added to the running QEMU instance. Windows-for-Mac commit
`e4c0c2b` adds an explicit `--ssh-forward-port` launch option, restricted to
shared NAT and loopback binding. A new app and a hash-bound launch profile have
been prepared, but the running VM has not yet switched to them. Actual
VM-restart connectivity therefore remains unqualified.

The guest also needs its local engine available after interactive login.
Anywhere Computer's existing `autostart-install` starts the HTTP/tunnel service;
it is not a local-only SSH-agent startup command. Do not apply it to an
SSH-only guest without the required HTTP configuration. A saved Plugin command
proves how the engine can be started by that client, not that Windows starts it
automatically after login. Verify both the host forwarding and guest startup
before relying on unattended reconnection. Do not restart that VM based on this
probe alone.
