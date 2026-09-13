# Isolated Windows integration testing

The Windows-for-Mac task owns VM lifecycle and guest command leases. Coordinate
with task `01a0777c-1d34-71d2-894f-9dccf0e7a55e` before guest commands. The
Anywhere Computer task owns the test payload and its result interpretation.

Use a consistent standalone clone or an overlay of an immutable, quiesced base.
Record the disk/base identity, VM ID, boot identity, QEMU executable digest and
accelerator, network mapping and guest architecture. Do not attach a writable
production disk to the test VM. Keep production credentials and user shares out
of the test environment. VM owners determine memory/disk budgets before boot.

Build `uv run python scripts/build_guest_test_bundle.py` on the host. The source
archive contains only explicitly selected source, tests, dependency lock and
project documentation. It contains no host configuration or credentials. Verify
its SHA-256 after transfer, then extract it into a new guest test folder. Check
all extracted file hashes against `bundle-manifest.json` before executing code.

In that folder, with uv available, run each command and record its exit code:

```powershell
uv sync --locked --python 3.12
uv run ruff check src tests
uv run mypy
uv run pytest -q
uv build
```

For terminal changes, first run the small real-process workflow below. It catches
command parsing, surviving children, Unicode file editing and repeated input in
one terminal before paying for a full Windows run:

```powershell
uv run pytest -v tests/test_terminal_children.py tests/test_file_search_terminal_workflow.py
```

The quality workflow runs native startup tests serially, then runs the remaining
Windows suite with two workers and keeps each test file on one worker:

```powershell
uv run pytest -ra tests/test_startup_native.py
uv run pytest -ra --ignore=tests/test_startup_native.py -n 2 --dist loadfile --max-worker-restart=0 --durations=20
```

Do not remove timeout, credential derivation or process cleanup assertions to
speed up a run. Compare JUnit timing from the same test inventory, report failures
and skips, and distinguish full-suite time from runner provisioning/build time.
`pytest-xdist` is a development dependency and is not added to the installed
Anywhere Computer runtime.

Record Python version and architecture and the bundle digest with test output.
These tests inject disposable in-memory authentication values. Real Windows
Credential Manager, MCP client reconnects, and host-to-guest transport require
additional integration runs; passing this suite does not prove those properties.
Never put real credentials into command arguments, environment, reports or ZIPs.

Next gates: native OS credential storage, agent startup, file conflict and
Unicode paths, interactive command input/output, client disconnection with
result recovery, agent failure without duplicate execution, authenticated
host/guest transport, rejected untrusted peers, then a genuinely external
network test. Host/guest NAT alone is not internet reachability evidence.

This test environment does not qualify HVCI or VRChat. Their separate live
acceptance tests remain owned by Windows-for-Mac.
