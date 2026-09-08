"""Verify complete local download-copy transfer with bounded buffers and cleanup."""

import argparse
import base64
import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from anywhere_computer.downloads import Downloads
from anywhere_computer.engine import runtime_identity
from anywhere_computer.models import BeginDownload, DownloadRange, TransferId


def verify(receipt: Path, mib: int) -> None:
    report = {
        "completed": False,
        "runtime_id": runtime_identity(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Full local download copy through Downloads API; no network or Engine ledger",
        "file_bytes": mib * 1024**2,
    }
    temporary_path = None
    try:
        with tempfile.TemporaryDirectory(prefix="anywhere-download-copy-") as temporary:
            temporary_path = Path(temporary)
            source = temporary_path / "source"
            block = bytes(range(256)) * 1024
            expected = hashlib.sha256()
            with source.open("wb") as output:
                for _ in range(mib * 4):
                    output.write(block)
                    expected.update(block)
                output.flush()
                os.fsync(output.fileno())
            downloads = Downloads(temporary_path)
            identity = uuid.uuid4().hex
            started = time.perf_counter()
            ready = downloads.begin(
                BeginDownload(
                    transfer_id=identity,
                    path=str(source),
                    expected_sha256=expected.hexdigest(),
                )
            )
            report["prepare_seconds"] = time.perf_counter() - started
            source.unlink()
            downloads = Downloads(temporary_path)
            offset, calls = 0, 0
            received = hashlib.sha256()
            started = time.perf_counter()
            while offset < ready["total_bytes"]:
                result = downloads.read(DownloadRange(transfer_id=identity, offset=offset))
                data = base64.b64decode(result["data_base64"])
                assert hashlib.sha256(data).hexdigest() == result["chunk_sha256"]
                received.update(data)
                assert result["next_offset"] > offset
                offset = result["next_offset"]
                calls += 1
            report["download_seconds"] = time.perf_counter() - started
            assert offset == mib * 1024**2 and received.hexdigest() == expected.hexdigest()
            report["chunks"] = calls
            report["sha256_matches"] = True
            report["closed"] = (
                downloads.close(TransferId(transfer_id=identity))["state"] == "closed"
            )
            report["completed"] = report["closed"]
    finally:
        report["temporary_directory_removed"] = (
            temporary_path is not None and not temporary_path.exists()
        )
        receipt.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--mib", type=int, default=1024, choices=range(1, 1025))
    args = parser.parse_args()
    verify(args.receipt, args.mib)
