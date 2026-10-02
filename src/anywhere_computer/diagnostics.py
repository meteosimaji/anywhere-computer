"""Read-only connection diagnosis without starting, stopping or repairing an agent."""

import ast
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import psutil
from pydantic import JsonValue

from . import __version__
from .browser_configuration import browser_configuration
from .capability_contract import CAPABILITY_TOOLS
from .codex_context import _executable
from .connection import exchange, load_endpoint
from .credentials import local_credential, secure_backend
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


def linux_credential_services() -> dict[str, JsonValue]:
    """List bus names only; never activate a wallet, inspect a collection or unlock it."""
    names = ("org.freedesktop.secrets", "org.kde.kwalletd5", "org.kde.kwalletd6")
    try:
        import_module("dbus")
        kwallet_binding = True
    except Exception:
        # An installed extension can still fail to import due to missing shared
        # libraries. No connection or password operation is performed here.
        kwallet_binding = False
    report: dict[str, JsonValue] = {
        "scope": "diagnostic_process_session_bus", "state": "not_checked",
        "services": {name: {"running": "unknown", "activatable": "unknown"} for name in names},
        "service_activation_requested": False, "unlock_state": "not_checked",
        "kwallet_python_dbus_available": kwallet_binding,
    }
    utility = next(((name, path) for name in ("gdbus", "busctl", "dbus-send")
                    if (path := shutil.which(name)) is not None), None)
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") or utility is None:
        report["reason"] = ("session_bus_environment_absent" if not os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS") else "dbus_probe_utility_unavailable")
        return report
    tool, executable = utility
    report["probe_tool"] = tool
    observed: dict[str, set[str]] = {}
    try:
        for method, key in (("ListNames", "running"), ("ListActivatableNames", "activatable")):
            if tool == "gdbus":
                command = [executable, "call", "--session", "--dest", "org.freedesktop.DBus",
                           "--object-path", "/org/freedesktop/DBus",
                           "--method", "org.freedesktop.DBus." + method]
            elif tool == "busctl":
                command = [executable, "--user", "--timeout=1s", "call", "org.freedesktop.DBus",
                           "/org/freedesktop/DBus", "org.freedesktop.DBus", method]
            else:
                command = [executable, "--session", "--dest=org.freedesktop.DBus", "--print-reply",
                           "--reply-timeout=1000", "/org/freedesktop/DBus",
                           "org.freedesktop.DBus." + method]
            result = subprocess.run(command, capture_output=True, timeout=2, check=False)
            if result.returncode or len(result.stdout) > 65536:
                raise ValueError("Unconfirmed session bus response")
            observed[key] = _credential_bus_names(tool, result.stdout.decode("utf-8"))
        report.update(state="observed", services={
            name: {key: name in found for key, found in observed.items()} for name in names
        })
    except (OSError, ValueError, SyntaxError, UnicodeError, subprocess.TimeoutExpired):
        report.update(state="unavailable", reason="session_bus_probe_failed")
    return report


def _credential_bus_names(tool: str, output: str) -> set[str]:
    """Accept the string-array shape from a known DBus utility; never return its other metadata."""
    if tool == "gdbus":
        # gdbus's empty string-array annotation is extra GVariant syntax.
        value = ast.literal_eval(output.replace("@as []", "[]"))
        if not isinstance(value, tuple) or len(value) != 1 or not isinstance(value[0], list):
            raise ValueError("Invalid session bus response")
        entries = value[0]
    elif tool == "busctl":
        fields = shlex.split(output)
        if (len(fields) < 2 or fields[0] != "as" or not fields[1].isdigit()
                or int(fields[1]) != len(fields) - 2):
            raise ValueError("Invalid session bus response")
        entries = fields[2:]
    else:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if (len(lines) < 3 or not lines[0].startswith("method return ")
                or lines[1] != "array [" or lines[-1] != "]"):
            raise ValueError("Invalid session bus response")
        entries = []
        for line in lines[2:-1]:
            match = re.fullmatch(r'string "([A-Za-z0-9_.:\-]+)"', line)
            if match is None:
                raise ValueError("Invalid session bus response")
            entries.append(match[1])
    if not all(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:\-]{1,255}", name)
               for name in entries):
        raise ValueError("Invalid session bus response")
    return set(entries)


def linux_prerequisites() -> dict[str, JsonValue]:
    """Inspect this process's session and select a secure backend without reading secrets."""
    credential_store: dict[str, JsonValue] = {
        "selection": "unavailable", "unlock_state": "not_checked",
        "credential_read": False, "credential_created": False,
        "plaintext_fallback": False,
        "next_action": "Use an unlocked Secret Service/KWallet in the same login session. "
                       "A shell's session environment does not describe another desktop session.",
    }
    try:
        backend = secure_backend()
        module = type(backend).__module__
        credential_store.update(selection="selected", backend=(
            "SecretService" if module.startswith("keyring.backends.SecretService") else
            "KWallet" if module.startswith("keyring.backends.kwallet") else "native_os_store"
        ))
    except Exception:
        # Backend discovery can raise provider-specific DBus errors. Never echo
        # those messages, switch stores, read a password or prompt to unlock.
        pass
    credential_services = linux_credential_services()
    services = credential_services.get("services")
    wallet_advertised = isinstance(services, dict) and any(
        isinstance(details, dict) and (details.get("running") is True
                                      or details.get("activatable") is True)
        for name, details in services.items() if name in {"org.kde.kwalletd5", "org.kde.kwalletd6"}
    )
    if (credential_store["selection"] == "unavailable" and wallet_advertised
            and credential_services.get("kwallet_python_dbus_available") is False):
        credential_store.update(
            reason="kwallet_python_binding_unavailable",
            next_action="KWallet is advertised but this Python cannot import dbus. "
                        "Use a qualified runtime with the KWallet binding or an operator-provided "
                        "Secret Service, then verify the existing store is unlocked. "
                        "No dependency, service or credential was created.",
        )
    session_type = os.environ.get("XDG_SESSION_TYPE")
    return {
        "scope": "diagnostic_process_only", "architecture": platform.machine(),
        "display_environment_present": bool(os.environ.get("DISPLAY")),
        "wayland_environment_present": bool(os.environ.get("WAYLAND_DISPLAY")),
        "session_bus_environment_present": bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")),
        "runtime_directory_environment_present": bool(os.environ.get("XDG_RUNTIME_DIR")),
        "session_type": session_type if session_type in {"x11", "wayland", "tty"} else "unknown",
        "desktop_session_verified": False,
        "credential_store": credential_store,
        "credential_services": credential_services,
        "platform_support": {
            "files": "implemented", "terminal": "implemented",
            "office_text_read_write": "implemented",
            "browser_isolated": "requires_playwright_and_chrome",
            "gui_native": "unsupported_platform", "audio_capture": "unsupported_platform",
            "documents_preview": "unsupported_platform",
        },
        "acceptance": "not_verified",
    }


def runtime_environment(directory: Path | None = None) -> dict[str, JsonValue]:
    """Inspect this process without starting task tools or disclosing environment values."""
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
    report: dict[str, JsonValue] = {
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
        'browser_configuration': browser_configuration(directory),
    }
    if sys.platform == "linux":
        report["linux_prerequisites"] = linux_prerequisites()
    return report


async def diagnose(directory: Path) -> dict[str, JsonValue]:
    source_runtime_id = runtime_identity()
    source_capabilities = source_capability_implementations()

    def report(state: str, action: str, **details: JsonValue) -> dict[str, JsonValue]:
        return {"state": state, "action": action, "changed": False,
                "source_build": {"version": __version__, "runtime_id": source_runtime_id},
                "source_capabilities": source_capabilities,
                "runtime_environment": runtime_environment(directory), **details}

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
    try:
        confirmed = await exchange(directory, "__status", credential=credential, timeout=3)
        if confirmed.state != "completed" or confirmed.data.get("state") != "ready":
            snapshot_state = "unconfirmed"
        elif (confirmed.data.get("instance_id") != agent.get("instance_id")
              or confirmed.data.get("runtime_id") != agent.get("runtime_id")):
            snapshot_state = "changed"
        else:
            snapshot_state = "same"
    except (OSError, ValueError, TimeoutError):
        snapshot_state = "unconfirmed"
    if snapshot_state != "same":
        # Status and catalog are separate reads. A restart between them must not
        # attribute the new engine's catalog to the earlier runtime snapshot.
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
            # A connection catalog can be filtered by publication or grants. It
            # cannot establish what code is installed in the running engine.
        details.setdefault("running_implementation", "unknown")
        details.setdefault("connection_authorization", "not_observed")
        details.setdefault("helper", "not_checked")
        details.setdefault("os_permission", "not_checked")
        details.setdefault("acceptance", "not_verified")
        capability_diagnostics[name] = details
    agent["capability_diagnostics"] = capability_diagnostics
    if snapshot_state == "changed":
        return report(
            "endpoint_changed", "Agent changed during diagnosis; rerun anywhere doctor.",
            agent=agent,
        )
    if snapshot_state == "unconfirmed":
        return report(
            "snapshot_unconfirmed",
            "Agent status could not be reconfirmed; rerun anywhere doctor.",
            agent=agent,
        )
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
        update_blocked = agent.get("update_blocked")
        if update_blocked is True:
            update_state = "blocked"
            update_action = (
                "Inspect the reported update_blocker_details and finish active work "
                "before running anywhere start. Other owners' resources may also block the "
                "switch; no session was stopped."
            )
        elif update_blocked is False:
            update_state = "idle"
            update_action = (
                "If the agent is still idle, run anywhere start to switch it to this build."
            )
        else:
            update_state = "unknown"
            update_action = (
                "Inspect active sessions and operations before running anywhere start; "
                "this runtime did not report whether an update is blocked."
            )
        return report(
            "different_build",
            update_action + " Feature authorization, helper permissions, and acceptance "
            "were not checked.",
            runtime_comparison=runtime_comparison,
            update_readiness={"state": update_state, "action": update_action}, agent=agent,
        )
    return report("ready", "No action required.",
                  runtime_comparison=runtime_comparison, agent=agent)
