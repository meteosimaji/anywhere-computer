"""Measure isolated KDF and Windows test-directory ACL cost on the CI runner.

Run before parallel pytest workers so setup cost is not confused with test-call
time or worker contention. All directories and password inputs are synthetic.
This diagnostic does not replace any security or application acceptance test.
"""

import argparse
import json
import os
import platform
import ssl
import statistics
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from anywhere_computer.owner_credentials import OwnerCredentials
from anywhere_computer.private_directory import migrate_default_windows_state
from anywhere_computer.state import prepare_directory


def measure(action: Callable[[], object], *, samples: int) -> dict[str, object]:
    durations = []
    for _ in range(samples):
        started = time.perf_counter()
        action()
        durations.append(time.perf_counter() - started)
    return {"samples_seconds": durations, "median_seconds": statistics.median(durations)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result: dict[str, object] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "openssl": ssl.OPENSSL_VERSION,
        "production_owner_kdf": measure(
            lambda: OwnerCredentials._derive("synthetic cost measurement", "ab" * 32),
            samples=3,
        ),
    }
    if os.name == "nt":
        # This is the same pre-created-directory migration used by tmp_path in
        # conftest.py. Validation runs after timing so it does not inflate the
        # migration measurement or silently accept an unusable state directory.
        with tempfile.TemporaryDirectory(prefix="anywhere-test-cost-") as directory:
            root = Path(directory)
            paths = [root / f"state-{index}" for index in range(20)]
            for path in paths:
                path.mkdir()
            remaining = iter(paths)
            result["windows_tmp_path_acl_migration"] = measure(
                lambda: migrate_default_windows_state(root=next(remaining), apply=True),
                samples=len(paths),
            )
            for path in paths:
                prepare_directory(path)
            result["windows_existing_private_directory_validation"] = measure(
                lambda: prepare_directory(paths[0]), samples=len(paths),
            )
    else:
        result["windows_tmp_path_acl_migration"] = {"not_applicable": True}
    rendered = json.dumps(result, indent=2) + "\n"
    arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
