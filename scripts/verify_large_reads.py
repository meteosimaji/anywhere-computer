"""Measure actual bounded-memory local reads; never extrapolate to a network benchmark."""

import argparse
import base64
import hashlib
import json
import os
import platform
import tempfile
import time
import tracemalloc
from pathlib import Path

import psutil

from anywhere_computer.engine import runtime_identity
from anywhere_computer.files import Files
from anywhere_computer.models import ReadBinary


def verify(receipt: Path, mib: int) -> None:
    report = {
        "completed": False,
        "runtime_id": runtime_identity(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": platform.system(),
        "scope": "Local Files API, generated regular file, three sampled ranges; not full download",
        "file_bytes": mib * 1024 * 1024,
        "measurements": [],
    }
    process = psutil.Process()
    temporary_path = None
    try:
        with tempfile.TemporaryDirectory(prefix="anywhere-large-read-") as temporary:
            temporary_path = Path(temporary)
            state = temporary_path / "state"
            state.mkdir()
            files = Files(state)
            source = temporary_path / "source.bin"
            # Write actual bytes, not a sparse truncate; bounded and reproducible content.
            block = bytes(range(256)) * 1024
            expected_hash = hashlib.sha256()
            with source.open("wb") as output:
                for _ in range(mib * 4):
                    output.write(block)
                    expected_hash.update(block)
                output.flush()
                os.fsync(output.fileno())
            total = source.stat().st_size
            for offset in (0, total // 2 + 127, total - 262144):
                rss_before = process.memory_info().rss
                tracemalloc.start()
                started = time.perf_counter()
                try:
                    result = files.read_binary(ReadBinary(
                        path=str(source), offset=offset, expected_sha256=expected_hash.hexdigest(),
                    ))
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                data = base64.b64decode(result["data_base64"])
                expected = bytes((offset + index) % 256 for index in range(len(data)))
                assert data == expected and result["total_bytes"] == total
                assert result["sha256"] == expected_hash.hexdigest()
                assert result["chunk_sha256"] == hashlib.sha256(data).hexdigest()
                assert result["eof"] == (offset + len(data) == total)
                report["measurements"].append({
                    "offset": offset, "returned_bytes": len(data), "seconds": elapsed,
                    "python_traced_peak_bytes": peak, "rss_before_bytes": rss_before,
                    "rss_after_bytes": process.memory_info().rss,
                })
            report["completed"] = True
    finally:
        report["temporary_directory_removed"] = (
            temporary_path is not None and not temporary_path.exists()
        )
        receipt.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--mib", type=int, default=1024, choices=range(1, 1025))
    args = parser.parse_args()
    verify(args.receipt, args.mib)
