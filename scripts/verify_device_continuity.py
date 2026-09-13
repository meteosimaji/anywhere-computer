"""Read-only continuity probe for an explicitly selected, already registered device."""

import argparse
import asyncio
import json
import re
import time
import uuid
from pathlib import Path

from pydantic import JsonValue

from anywhere_computer.device_router import DeviceRouter
from anywhere_computer.models import Reply, Request


async def verify(directory: Path, device: str, path: str, sha256: str,
                 operation_id: str) -> dict[str, JsonValue]:
    async def catalog() -> list[JsonValue]:
        return []

    async def local(_: Request) -> Reply:
        raise ValueError("This probe requires an explicit remote device")

    router = DeviceRouter(directory, catalog, local)
    report: dict[str, JsonValue] = {"device_id": device, "checks": {}}
    checks: dict[str, JsonValue] = {}
    probes: tuple[tuple[str, dict[str, JsonValue]], ...] = (
        ("computer_status", {}),
        ("files_read", {"path": path}),
        ("operations_get", {"operation_id": operation_id}),
    )
    try:
        for tool, arguments in probes:
            started = time.monotonic()
            reply = await router.execute(Request(
                operation_id=uuid.uuid4().hex, tool="devices_call",
                arguments={"device_id": device, "tool": tool, "arguments": arguments},
            ))
            result = reply.data.get("result")
            entry: dict[str, JsonValue] = {
                "state": reply.state,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
            checks[tool] = entry
            if reply.state != "completed" or not isinstance(result, dict):
                entry["error"] = reply.error
                report.update(checks=checks, passed=False)
                return report
            if tool == "computer_status":
                for key in ("platform", "instance_id", "runtime_id", "version"):
                    entry[key] = result.get(key)
            elif tool == "files_read":
                entry["sha256_matches"] = result.get("sha256") == sha256
                if not entry["sha256_matches"]:
                    report.update(checks=checks, passed=False)
                    return report
            else:
                entry["original_state"] = result.get("state")
                entry["operation_id_matches"] = result.get("operation_id") == operation_id
                if result.get("state") != "completed" or not entry["operation_id_matches"]:
                    report.update(checks=checks, passed=False)
                    return report
        report.update(checks=checks, passed=True)
        return report
    finally:
        router.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-directory", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{32}", args.device):
        parser.error("Select a registered remote device ID")
    if not re.fullmatch(r"[a-f0-9]{64}", args.sha256):
        parser.error("Expected a lowercase SHA-256 digest")
    if not (args.device_directory / "devices.sqlite3").is_file():
        parser.error("The existing device registry was not found")
    report = asyncio.run(verify(args.device_directory, args.device, args.path,
                                args.sha256, args.operation_id))
    print(json.dumps(report, ensure_ascii=True, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
