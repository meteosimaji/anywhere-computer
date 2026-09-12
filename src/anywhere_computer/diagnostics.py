"""Read-only connection diagnosis without starting, stopping or repairing an agent."""

import shutil
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import psutil
from pydantic import JsonValue

from .connection import exchange, load_endpoint
from .credentials import local_credential
from .runtime_identity import runtime_identity


def runtime_environment() -> dict[str, JsonValue]:
    """Inspect this process, without launching tools or disclosing environment values."""
    try:
        sdk_version = version('mcp')
    except PackageNotFoundError:
        sdk_version = None
    executables: dict[str, JsonValue] = {
        name: shutil.which(name) for name in ('node', 'uv', 'codex')
    }
    return {
        'scope': 'diagnostic_process',
        'python': sys.executable,
        'python_version': sys.version.split()[0],
        'executables_on_path': executables,
        'mcp_sdk_version': sdk_version,
        'direct_mcp_dependency': 'ready' if sdk_version == '1.30.0' else (
            'missing' if sdk_version is None else 'untested_version'),
        'direct_mcp_action': 'No dependency action required.' if sdk_version == '1.30.0' else
            'From source, run uv sync --locked --extra mcp; then use that Python runtime.',
        'browser_operation_verified': False,
    }


async def diagnose(directory: Path) -> dict[str, JsonValue]:
    def report(state: str, action: str, **details: JsonValue) -> dict[str, JsonValue]:
        return {"state": state, "action": action, "changed": False,
                "runtime_environment": runtime_environment(), **details}

    try:
        endpoint = load_endpoint(directory)
    except FileNotFoundError:
        return report("stopped", "Run anywhere start to initialize or start the agent.")
    except (OSError, ValueError):
        return report(
            "invalid_endpoint",
            "Check access to the state directory and agent.json; preserve existing work.",
        )
    pid = endpoint.get("pid")
    created = endpoint.get("process_started")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(created, (int, float))
        or isinstance(created, bool)
    ):
        return report("invalid_endpoint", "Inspect agent.json; no process was changed.")
    try:
        if psutil.Process(pid).create_time() != created:
            return report("stale_endpoint", "Run anywhere start; the recorded agent has exited.")
    except psutil.NoSuchProcess:
        return report("stale_endpoint", "Run anywhere start; the recorded agent has exited.")
    except psutil.AccessDenied:
        return report("process_access_denied", "Run under the account that started the agent.")
    try:
        credential = local_credential(directory)
    except (OSError, RuntimeError):
        return report(
            "credential_unavailable",
            "Unlock the OS credential store in this login session, then rerun anywhere doctor.",
            process_alive=True,
        )
    try:
        reply = await exchange(directory, "__status", credential=credential, timeout=3)
    except (OSError, ValueError, TimeoutError):
        return report(
            "unresponsive",
            "The recorded process is alive but authenticated status failed. "
            "Preserve active work; check its login session and rerun anywhere doctor.",
            process_alive=True,
        )
    if reply.state != "completed" or reply.data.get("state") != "ready":
        return report("not_ready", "Agent responded without readiness; retry diagnosis.")
    if reply.data.get("instance_id") != endpoint.get("instance_id"):
        return report("endpoint_changed", "Connection metadata changed; rerun anywhere doctor.")
    if reply.data.get("runtime_id") != runtime_identity():
        return report(
            "different_build",
            "Finish active work with the previous installation, then run anywhere start "
            "to switch an idle agent to this build.",
            agent=reply.data,
        )
    return report("ready", "No action required.", agent=reply.data)
