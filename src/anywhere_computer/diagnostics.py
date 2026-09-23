"""Read-only connection diagnosis without starting, stopping or repairing an agent."""

import ast
import os
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import psutil
from pydantic import JsonValue

from . import __version__
from .capability_contract import CAPABILITY_TOOLS
from .codex_context import _executable
from .connection import exchange, load_endpoint
from .credentials import local_credential
from .execution_environment import with_tool_path
from .runtime_identity import runtime_identity


def source_capability_implementations() -> dict[str, JsonValue]:
    """Read literal tool registrations from this installed source without creating an engine."""
    try:
        source = Path(__file__).with_name("engine.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeError):
        return {name: "unknown" for name in CAPABILITY_TOOLS}
    registered = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
        and node.func.attr == "register" and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    return {
        name: "present" if required <= registered else "absent"
        for name, required in CAPABILITY_TOOLS.items()
    }


def runtime_environment() -> dict[str, JsonValue]:
    """Inspect this process, without launching tools or disclosing environment values."""
    try:
        sdk_version = version('mcp')
    except PackageNotFoundError:
        sdk_version = None
    executables: dict[str, JsonValue] = {
        name: shutil.which(name) for name in ('node', 'uv', 'codex')
    }
    child_path = next((value for key, value in with_tool_path(os.environ).items()
                       if key.upper() == 'PATH'), '')
    child_executables: dict[str, JsonValue] = {
        name: shutil.which(name, path=child_path) for name in ('node', 'uv', 'codex')
    }
    codex_selection: dict[str, JsonValue] = {
        'source': 'explicit_override' if os.environ.get('ANYWHERE_CODEX_EXECUTABLE') else 'path',
        'state': 'unresolved', 'path': None, 'execution_verified': False,
    }
    try:
        codex_selection.update(state='resolved', path=str(_executable(None)))
    except (OSError, ValueError):
        # Use the same selection contract as execution, without launching Codex
        # or printing a malformed override/exception containing private values.
        pass
    return {
        'scope': 'diagnostic_process',
        'python': sys.executable,
        'python_version': sys.version.split()[0],
        'executables_on_path': executables,
        'executables_for_new_children': child_executables,
        'codex_selection': codex_selection,
        'mcp_sdk_version': sdk_version,
        'direct_mcp_dependency': 'ready' if sdk_version == '1.30.0' else (
            'missing' if sdk_version is None else 'untested_version'),
        'direct_mcp_action': 'No dependency action required.' if sdk_version == '1.30.0' else
            'From source, run uv sync --locked --extra mcp; then use that Python runtime.',
        'browser_operation_verified': False,
    }


async def diagnose(directory: Path) -> dict[str, JsonValue]:
    source_runtime_id = runtime_identity()
    source_capabilities = source_capability_implementations()

    def report(state: str, action: str, **details: JsonValue) -> dict[str, JsonValue]:
        return {"state": state, "action": action, "changed": False,
                "source_build": {"version": __version__, "runtime_id": source_runtime_id},
                "source_capabilities": source_capabilities,
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
    agent = dict(reply.data)
    raw_diagnostics = agent.get("capability_diagnostics")
    capability_diagnostics: dict[str, JsonValue] = {
        name: dict(value) for name, value in raw_diagnostics.items()
        if isinstance(name, str) and isinstance(value, dict)
    } if isinstance(raw_diagnostics, dict) else {}
    try:
        catalog_reply = await exchange(directory, "__catalog", credential=credential, timeout=3)
        catalog = catalog_reply.data.get("tools")
        catalog_names = {
            row["name"] for row in catalog
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        } if catalog_reply.state == "completed" and isinstance(catalog, list) else None
    except (OSError, ValueError, TimeoutError):
        catalog_names = None
    for name, required in CAPABILITY_TOOLS.items():
        raw = capability_diagnostics.get(name)
        details = dict(raw) if isinstance(raw, dict) else {}
        details["source_implementation"] = source_capabilities[name]
        if catalog_names is None:
            details["connection_publication"] = "unknown"
        else:
            published = required & catalog_names
            details["connection_publication"] = (
                "published" if published == required else
                "partial" if published else "not_published"
            )
            details.setdefault("running_implementation", (
                "present" if published == required else
                "partial" if published else "absent"
            ))
        details.setdefault("running_implementation", "unknown")
        details.setdefault("connection_authorization", "not_observed")
        details.setdefault("helper", "not_checked")
        details.setdefault("os_permission", "not_checked")
        details.setdefault("acceptance", "not_verified")
        capability_diagnostics[name] = details
    agent["capability_diagnostics"] = capability_diagnostics
    agent_version = reply.data.get("version")
    agent_runtime_id = reply.data.get("runtime_id")
    same_version = isinstance(agent_version, str) and agent_version == __version__
    same_runtime = isinstance(agent_runtime_id, str) and agent_runtime_id == source_runtime_id
    runtime_comparison: dict[str, JsonValue] = {
        "state": "same" if same_runtime else "different" if isinstance(agent_runtime_id, str)
        else "unknown",
        "version_matches": same_version if isinstance(agent_version, str) else None,
        "runtime_id_matches": same_runtime if isinstance(agent_runtime_id, str) else None,
        "source_implementation": "current_diagnostic_process",
        "connection_authorization": "authenticated_status_only",
        "feature_authorization": "unknown",
        "helper_and_os_permissions": "not_checked",
        "acceptance": "not_verified",
    }
    if not same_runtime:
        return report(
            "different_build",
            "Finish active work with the previous installation, then run anywhere start "
            "to switch an idle agent to this build. Feature authorization, helper permissions, "
            "and acceptance were not checked.",
            runtime_comparison=runtime_comparison, agent=agent,
        )
    return report("ready", "No action required.",
                  runtime_comparison=runtime_comparison, agent=agent)
