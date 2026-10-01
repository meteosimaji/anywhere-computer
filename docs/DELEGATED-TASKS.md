# Delegated task authorization status

The HTTP server instantiates `DelegatedTaskStore` and can accept an owner-issued
child bearer. An owner can use a local terminal to list parent grants, issue a
child credential, and revoke it:

```text
anywhere http-delegate-list
anywhere http-delegate-issue --parent-grant-id PARENT_ID --scope files_read \
  --read-root /absolute/existing/directory --expires-in 3600
anywhere http-delegate-issue --parent-grant-id PARENT_ID --scope files_read \
  --read-file /absolute/existing/file.txt --expires-in 3600
anywhere http-delegate-revoke --child-id CHILD_ID
```

Each action prompts for the current owner password. The issue command prints the
bearer once; keep it out of logs and chat prompts. `DelegatedTaskStore.issue` returns
the bearer once; only its SHA-256 digest is saved. The child bearer is bound to a
parent OAuth grant, owner, child ID, one device ID, allowed tools, canonical file
read/write roots, individual read files, and an expiry. Revocation and parent
grant state are read from storage on each delegated operation. A refusal is recorded in the separate
delegated operation ledger and an audit table without paths, tokens, or argument
values. The bearer must be delivered to a child MCP client through a trusted
owner-controlled channel. `--delegated-device-id` selects one device; the
default is `local`. Repeat `--scope`, `--read-root`, `--read-file`, and `--write-root` as
needed. Local roots must be existing canonical absolute directories, including `/` on POSIX.
A root grant still rejects symlinks in every component. Individual
read files must be existing canonical absolute regular files. A `--read-file`
entry grants only that path to `files_read`, never siblings, the containing
directory, or descendants if the file is replaced by a directory. Symlinks in
the file or any ancestor are rejected by the executor, including substitutions
after the policy check. File contents can change at the selected path; this is
path access, not a hash-pinned content grant. Old grants without `read_files`
retain their existing roots with no additional paths. The maximum
lifetime is 24 hours, and the parent grant may end access earlier.
An individual-file child also uses the currently selected Engine's read-line
limit. The shared-agent path reads that Engine's settings on each operation;
it does not use the older control ledger's defaults. A pending Engine migration,
missing selected Engine, or invalid settings denies the read before the file
executor runs, and its failed receipt remains available through `operations_get`
when that recovery scope is granted.

The isolated regressions in `tests/test_delegated_selected_files.py` and
`test_shared_delegated_files_use_selected_engine_limits` cover the file boundary,
HTTP reconnect/revocation, selected-Engine limits and refusal during settings
failures. They do not establish an actual ChatGPT dot's credential handoff or
end-to-end acceptance. A normal OAuth grant with `files_read` alone remains
tool-scoped; it does not acquire the child grant's path restrictions. Delivering
a child bearer to a dot still requires a supported, trusted client configuration
path, without putting the bearer in prompts or chat.

Every child grant containing `files_write` must also contain `operations_get`,
with both tools present in the parent grant for a local child. An older local
child grant lacking recovery scope becomes inactive and must be reissued. The
target checks this invariant again when a file worker starts, including after
the source has already read its tool catalog.

Remote file calls require a separately issued target child grant on an HTTP or
SSH target. Issue that target grant with `device_id=local` and target-local roots.
Then register the target on the source, issue a source child
grant for its registered device ID and intended tools without source roots, and
bind the target child bearer on the source:

```text
anywhere http-delegate-route --child-id SOURCE_CHILD_ID \
  --target-child-id TARGET_CHILD_ID
```

The command prompts for the source owner password and target bearer without
putting either in arguments or output. The target child ID is copied from the
target owner's issuance result; it is non-secret route metadata, not proof that
the bearer matches that ID. The route identity, target child ID, and target
resource are saved in SQLite; the bearer goes in the native credential store.
Binding checks the source child and registered target identity locally; the
target's authenticated `delegated_identity` result is compared with the saved
target child ID before each forwarded operation. A missing or mismatched ID
fails before the requested remote side effect. Existing routes without a
recorded ID must be replaced using a new source child and route to send again.
For an SSH target, configure a verified SSH host alias and the same Anywhere
Computer HTTP authorization state on the target. The source opens
`anywhere ssh-child-mcp` over strict-host-key SSH. The target child bearer is
sent only in the authenticated stdio channel after SSH connects; it is never
placed in the SSH command, environment, MCP tool arguments, or prompt. The
target requires child authentication before MCP initialization and checks the
grant again for every call. Authentication failure closes the source process
and never switches to ordinary `anywhere mcp`.
`http-delegate-unroute --child-id SOURCE_CHILD_ID` removes the local route.
`http-delegate-revoke` also removes it when revoking the source child. Remote
`devices_call` now uses the target child bearer instead of the saved broad
OAuth connection or ordinary SSH MCP session. The target checks its own child
grant and confines paths before side effects. Missing routes and changed
HTTP resources or SSH aliases fail closed. The source checks its child, device,
tool and expiry before routing.
The source rechecks the child grant, parent grant and saved target route after
remote catalog discovery and immediately before handing the call to the
target transport. A revocation or expiry completed while catalog discovery
waited now produces a recorded, undispatched failure. This source check cannot
cancel a request that has already entered the transport; revoke the target
child grant as well when stopping its independent access.
Revoking the source grant stops new source calls; revoke the separately issued
target child grant on the target to end its independent access there.
The owner administration result reports `source_child=revoked` and, for a
remote child, `source_route=revoked` with
`target_child_grant=not_revoked_here`. The `revoked=true` compatibility field
describes only the source child. A call already handed to the target transport
may finish after the source revoke; target-side revocation is a separate owner
action and must be checked on that device. The source result includes the
recorded `target_child_id` so the target owner can select the exact grant with
`http-delegate-revoke --child-id TARGET_CHILD_ID` on the target, then check its
`active=false` entry with `http-delegate-list`. A missing ID on a legacy route,
an unreachable target, or a failed target check leaves target revocation
unconfirmed. The source `http-delegate-list` retains the recorded target ID
for owner recovery after the revoke output is lost; it never reports target
revocation as confirmed.

The implemented HTTP and SSH child sessions authenticate their own bearers.
They can use local `files_read`, `files_write`, `operations_get`, and
`computer_status` only when each tool is explicitly in the grant and in the
active parent grant. Reconnects use the same child ID and current grant checks.
The local file worker rechecks the child and parent grants after the async
handoff. It holds reservations on both authorization databases while the
descriptor-confined read or write runs. A revocation that wins before the
worker starts denies the call and is recorded in the audit; a revocation that
arrives during the operation waits for that operation to finish. This boundary
does not add a child identity to ordinary ChatGPT. The routed HTTP path uses
the target's independently authenticated child identity.

This is not an ordinary Subchat feature yet. `subchat_send` starts an ordinary
ChatGPT Chat and its optional plugin references add observed `plugin://` URIs
and hints to the prompt. The Subchat transport has no authenticated channel for
installing a child bearer into that Chat's MCP connection. Incoming HTTP MCP
requests carry the OAuth connection grant, but no verified Subchat conversation
or child task identity to bind to a child grant. Putting a bearer in the prompt
or plugin URI would expose it to Chat content and history; selecting the normal
connector would instead use its existing broader OAuth grant. A separate,
verified child connection and a safe way to select it for exactly one Chat are
needed before ordinary Subchat can operate under this contract. Natural-language
task instructions are never access control.

Child operation IDs are namespaced before execution, and `operations_get` maps a
child's own ID back for recovery. Remote child calls have a source-side
operation record. A repeated remote
call with the same ID and arguments returns the saved result; if its outcome
was not recorded, it returns `unknown` and requires `operations_get` instead
of sending the call again. Remote failures are audited with a generic reason
that does not store target paths or error text.
Before target dispatch, the source saves the selected device, tool,
authenticated target child ID, and digest of the exact target request.
The target's `operations_get` reports its saved tool and request digest for both
file-ledger and engine-backed tools, such as `computer_status`, to
the authenticated child. When those and the operation ID agree, the source
can reconcile a terminal target result with
its running or unknown original record; an exact retry then returns the saved
result without dispatch. A changed target child ID or a different request
under the same target operation ID blocks reconciliation.
Operations predating this binding remain unknown when their source result was
not recorded. Both source and target children need `operations_get` permission
to recover an uncertain target result. The source also checks that the target child
publishes `operations_get` before forwarding a write. Never retry a write with
a new ID.

A newer HTTP gateway can reuse a selected older Engine without replacing its
active work. Catalog and ordinary calls omit the optional receipt extension so
the beta.48 strict loopback schema still accepts them. Before requesting an
Engine-backed child receipt, the gateway authenticates the live Engine's
`engine_protocol_features` declaration and requires `remote_receipt_metadata`.
It checks child expiry and child/parent revocation again after that probe.
An absent or unsupported declaration returns `engine_feature_unavailable` with
`dispatched=false`; update the selected Engine through its launcher and look
up the original operation ID. Never resend the original request to bypass this
refusal. A failed probe does not select an older protocol or accept an unbound
receipt. The child's file ledger can still recover its own local file results
without that Engine extension.

Local child `files_read` and `files_write`
(create, replace, append) use descriptor-relative traversal from a configured
root through the target parent, reject symlinks in every component, and update
inside that pinned parent directory. Delegated writes use the selected engine's
ordinary file mutation locks across hash checking and publication, preventing
both callers from acknowledging conflicting writes against the same original hash.
This prevents an ancestor symlink swap
after the policy precheck from reaching another tree. Other path tools are
refused for delegated children until they have equivalent executor confinement;
issuing a new grant with those scopes is also refused. On hosts without the
required POSIX descriptor APIs these file calls fail closed. Backup copies for
child writes live in the delegation state directory; child `files_restore` is
not currently available. The local Engine's current file line limits also
apply to delegated calls; when using a shared agent, the HTTP service reads
those limits from the agent's SQLite ledger before dispatch and refuses the
file call if it cannot read them. An end-to-end ordinary Subchat credential handoff and
safe implementations of the remaining path tools are required for closing #180.
