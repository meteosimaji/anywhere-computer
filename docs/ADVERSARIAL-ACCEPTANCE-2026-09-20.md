# Adversarial acceptance, 2026-09-20

Development base: 9ff2426, plus the 32-dispatcher case in test_subchat_delivery.py.
Isolated macOS processes, SQLite stores and Chrome contexts were used; no user
browser, installed service or production credentials were terminated.

- Full suite, three workers: 1455 passed, 18 skipped, 68.07 seconds.
- Recovery subset: 30 cases repeated 20 times, all 600 executions passed.
- Each repetition includes 32 concurrent attempts through separate SQLite
  connections to deliver one queued message: exactly one provider send.
- Existing cases cover committed-write response loss, retained stdio/HTTP clients
  after isolated engine kill/stop, grant revocation during reconnect, cancellation
  during preparation, unknown submission after restart and conflicting IDs.
- The closure regression uses real isolated Chrome and preserves saved submissions.

Commands:

```sh
uv run pytest -q -n 3 --dist loadfile
uv run pytest -q tests/test_subchat_delivery.py tests/test_subchat_lifecycle.py tests/test_shared_agent_reconnect.py tests/test_operation_acceptance.py
```

The second command was run 20 times in separate pytest processes. This is a bounded
repeatability check, not an exhaustive schedule explorer or production load test.
The 32-dispatcher test uses a deterministic provider with real SQLite connections,
not 32 live ChatGPT sends. Full-suite skips were Linux/Windows native validators,
Windows console/pipe cases and runner-only native registration. No browser tests
were skipped. Real provider limits, long-duration service faults, OS reboot and
Windows runtime acceptance remain separate evidence.
