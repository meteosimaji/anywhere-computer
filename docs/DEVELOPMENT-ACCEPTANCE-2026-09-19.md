# Development runtime acceptance and recovery review

The tested source baseline is main `5d7522ff82e35fd16af6a76cc61d107530a679da`.
Python runtime version is `0.2.0a1`, runtime identity
`7e007ed62aa06e120479abb5622a288874f155fbad04625d0c86008df5dd22e5`.
This is development acceptance, not another beta release.

## Automated ordinary-Chat wire workflow

`tests/test_development_http_workflow.py` uses real authenticated loopback HTTP MCP,
temporary files, a real Python subprocess and a persistent operation ledger. It
runs without a Codex or GUI fixture and is not restricted to POSIX. It discovers
the granted tool schemas and checks the actual runtime identity before work.

The client writes a Python source file, reads it, edits one value using its SHA-256,
runs the edited file, verifies stdout `3501` and exit code 0, and retains an output
operation receipt. It then closes the engine and HTTP server, creates a replacement
engine against the same state directory, initializes a new MCP session, and recovers
the exact saved output. Repeating the original start operation ID must return its
saved result without starting another terminal. A file written by the executed
script independently verifies exactly one execution.

This tests a clean process/service restart, not OS reboot, suspend or abrupt power
loss. The broader `test_http_capability_acceptance.py` exercises all remotely
available engine tools but uses synthetic Codex/GUI peers; those peers are not real
third-party application acceptance.

## Packaged runtime and local deployment

An offline portable build using the checked-in wheel completed. The relocated
runtime passed `verify_portable.py --runtime-only` (file round trip and regex child;
3,132 manifest files). Its runtime identity matched the source identity above.
The previous beta runtime was idle before switching the existing shared engine to
the development package. The existing local MCP connection subsequently returned
`0.2.0a1` and the matching identity. The HTTP startup registration was upgraded to
the same package and started. Both loopback and public metadata became reachable.
Metadata alone does not prove an authenticated Chat tool call.

## SSH and restart boundaries from current implementation

- `ssh_transport.py` uses an existing OpenSSH alias with strict host-key checking,
  noninteractive authentication, a 10-second connection timeout and 15-second
  keepalives (three missed replies). It does not install or start a remote SSH
  service, wake hardware, or power on a stopped machine.
- `ssh_client.py` negotiates recoverable operation IDs and retains the operation ID
  over the SSH MCP boundary. It does not blindly reconnect and replay a request
  whose response is missing. Status checks can report unreachable without proving
  whether the cause is power, network, SSH startup or the remote command.
- Current macOS registration is a user LaunchAgent; Windows registration is an
  interactive-user logon task. These are not guarantees of pre-login service
  availability after reboot. Linux uses a user service as well.
- Persistent startup policy restarts failed supervisors. Native credential-store
  access, networking and the user's session must still be available. Process memory,
  terminal processes and plugin sessions are not restored by the operation ledger.
- A stopped Windows VM requires a separate running host to start it, then guest
  readiness and authenticated reconnection checks. This is not currently supplied
  by SSH itself. A powered-off physical PC similarly requires a supported external
  power-on mechanism; no such capability is claimed here.

Required remaining hardware acceptance includes OS reboot/login, sleep/resume,
network interruption and Windows VM stop/start. Do not label a failed connection
as sleep solely because the display is black. A recovered ledger receipt is a
historical result, not proof that a GUI or terminal process survived reboot.

## Live ordinary Chat and service recovery

A fresh ordinary Chrome Chat with GPT-5.6 Sol / medium selected ran marker
`AC_DEV_20260919_D` against the development engine. Its initial and final status
reported the exact version and runtime above. The temporary source file was
independently read and its SHA-256 matched the Chat's reported edited hash.
More importantly, an authenticated HTTP `operations_get` independently recovered
its terminal-output receipt: `3501\n`, exit code 0, EOF true, dropped bytes 0.
No inference is made solely from the assistant's success prose.

The Chat reported host-side approval interruptions before some operations. Its
reported count is internally inconsistent, so no exact count is asserted here.
The host's approval policy was not disabled or modified. This workflow does not
establish uninterrupted autonomous operation under every host policy.

With active resources at zero, the shared engine was explicitly stopped through
the installed development CLI. The existing authenticated HTTP connection then
returned a new engine instance ID with the same runtime identity. The same output
operation receipt was recovered again without rerunning the Python source. This
establishes recovery after a controlled engine stop on this Mac; it is not an OS
reboot, physical power-on, Windows or network-failure acceptance claim.

Local checks: 51 targeted HTTP/capability/subchat/search/workflow tests passed;
59 SSH/startup/shared-agent-reconnection tests passed with 3 platform-specific
skips. Ruff passed for src/tests/scripts and mypy passed for 98 source files.

The original ordinary Chat subsequently performed only status and receipt lookup,
and independently reported the new instance ID and the same saved stdout/exit code.
It did not create another terminal or rerun the source for this recovery check.

## Windows CI correction

The first Windows run reached the final duplicate-execution check but failed
because the fixture's text-mode append translated LF to CRLF. It reported
1,280 passed, 32 skipped and this one failure. The generated script now explicitly
disables newline translation for its execution marker; the exact one-marker
assertion and the real HTTP/file/subprocess/restart workflow are unchanged.
The corrected test passed locally; Windows confirmation requires the new CI run.
