"""Management model shared by local CLI and desktop hosts.

No command execution surface is exported to a WebView. Saved configuration and
cached device observations never constitute an authenticated live connection.
"""

import asyncio
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from .connection import ensure_agent
from .devices import DeviceStore
from .diagnostics import diagnose
from .models import Contract
from .release_supervisor import automatic_updates_enabled
from .setup_controller import SetupController, SetupProgress
from .startup_service import install_startup, startup_status, uninstall_startup


class ManagementStartup(Contract):
    schema_version: Literal[1] = 1
    state: Literal["not_installed", "registered", "not_enabled", "unavailable"]
    mode: Literal["local", "remote"] | None = None
    native_running: bool = False
    observed_at: str


class ManagementStartupResult(Contract):
    schema_version: Literal[1] = 1
    state: Literal["confirmed", "not_confirmed", "unsupported_mode"]
    startup: ManagementStartup


class ManagedDevice(Contract):
    device_id: str
    name: str
    transport: str
    observation_source: Literal["cached"] = "cached"
    last_observed_state: str
    checked_at: float | None = None
    # A cached ready state must never light up the live-ready indicator.
    live_state: Literal["not_checked"] = "not_checked"


class ManagementSnapshot(Contract):
    schema_version: Literal[1] = 1
    observed_at: str
    engine_state: str
    next_action: str
    version: str | None = None
    runtime_id: str | None = None
    instance_id: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    active_resources: dict[str, int] = Field(default_factory=dict)
    setup: SetupProgress
    remote_connection: Literal["not_checked"] = "not_checked"
    devices: list[ManagedDevice] = Field(default_factory=list)
    device_registry_state: Literal["absent", "read", "unavailable"]
    automatic_stable_updates: bool
    update_preference_scope: Literal["saved_configuration"] = "saved_configuration"
    changed: Literal[False] = False


class ManagementDeviceCheck(Contract):
    schema_version: Literal[1] = 1
    device_id: str
    state: str
    observed_at: str
    transport: str
    evidence: Literal["agent_status", "authorized_catalog"]


class ManagementStartResult(Contract):
    schema_version: Literal[1] = 1
    state: Literal["ready", "not_confirmed"]
    snapshot: ManagementSnapshot
    action: str


def _text(data: dict[str, JsonValue], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


class ManagementController:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.setup = SetupController(directory)

    async def check_device(self, device_id: str) -> ManagementDeviceCheck:
        if re.fullmatch(r"[a-f0-9]{32}", device_id) is None:
            raise ValueError("Select a registered device ID")

        def probe() -> ManagementDeviceCheck:
            if not (self.directory / "devices.sqlite3").is_file():
                raise ValueError("No device registry exists")
            store = DeviceStore(self.directory)
            try:
                device = store.get(device_id)
                http = device["transport"] == "http"
                result = (asyncio.run(store.probe_http(device_id))
                          if http else store.probe(device_id))
                return ManagementDeviceCheck(
                    device_id=device_id, state=str(result["last_observed_state"]),
                    transport=str(result["transport"]),
                    observed_at=datetime.now(UTC).isoformat(),
                    evidence="authorized_catalog" if http else "agent_status",
                )
            finally:
                store.close()

        return await asyncio.to_thread(probe)

    async def startup(self) -> ManagementStartup:
        try:
            raw = await asyncio.to_thread(startup_status, self.directory)
            state = raw.get("state")
            mode = raw.get("mode")
            if state not in {"not_installed", "registered", "not_enabled"}:
                raise ValueError("Unrecognized startup state")
            return ManagementStartup.model_validate({
                "state": state, "mode": mode,
                "native_running": raw.get("native_running", False),
                "observed_at": datetime.now(UTC).isoformat(),
            })
        except (OSError, RuntimeError, ValueError):
            return ManagementStartup(
                state="unavailable", observed_at=datetime.now(UTC).isoformat(),
            )

    async def set_local_startup(self, enabled: bool) -> ManagementStartupResult:
        before = await self.startup()
        if before.mode == "remote":
            return ManagementStartupResult(state="unsupported_mode", startup=before)
        if before.state == "unavailable":
            return ManagementStartupResult(state="not_confirmed", startup=before)
        try:
            if enabled:
                await asyncio.to_thread(install_startup, self.directory, mode="local")
            else:
                await asyncio.to_thread(uninstall_startup, self.directory, expected_mode="local")
        except (OSError, RuntimeError, ValueError):
            # An acknowledgement failure does not prove that the OS mutation
            # failed. Read once; never repeat the mutation automatically.
            pass
        after = await self.startup()
        confirmed = (after.state == "registered" and after.mode == "local") if enabled else (
            after.state == "not_installed"
        )
        return ManagementStartupResult(
            state="confirmed" if confirmed else "not_confirmed", startup=after,
        )

    async def start(self) -> ManagementStartResult:
        # Starting is explicit. Keep the existing selected runtime and busy-work
        # rules; opening the manager must not silently activate another build.
        try:
            await asyncio.to_thread(ensure_agent, self.directory)
        except (OSError, RuntimeError, ValueError, TimeoutError):
            # The start acknowledgement may be lost after the engine starts.
            # Reconcile real status instead of repeating the start or claiming
            # that an exception proves no process was created.
            pass
        observed = await self.snapshot()
        ready = observed.engine_state == "ready"
        return ManagementStartResult(
            state="ready" if ready else "not_confirmed",
            snapshot=observed,
            action="Engine responded; inspect the observed runtime before work."
            if ready else "Start was not confirmed; inspect diagnosis before retrying.",
        )

    async def snapshot(self) -> ManagementSnapshot:
        diagnosis = await diagnose(self.directory)
        raw = diagnosis.get("agent")
        agent = raw if isinstance(raw, dict) else {}
        devices: list[ManagedDevice] = []
        registry_state: Literal["absent", "read", "unavailable"] = "absent"
        registry = self.directory / "devices.sqlite3"
        if registry.exists() or registry.is_symlink():
            store: DeviceStore | None = None
            try:
                store = DeviceStore(self.directory, read_only=True)
                for device in store.list():
                    devices.append(ManagedDevice(
                        device_id=str(device["device_id"]), name=str(device["name"]),
                        transport=str(device["transport"]),
                        last_observed_state=str(device["last_observed_state"]),
                        checked_at=(device["checked_at"]
                                    if isinstance(device["checked_at"], float) else None),
                    ))
                registry_state = "read"
            except (OSError, ValueError, sqlite3.Error):
                devices = []
                registry_state = "unavailable"
            finally:
                if store is not None:
                    store.close()
        capabilities = agent.get("capabilities")
        resources = agent.get("active_resources")
        return ManagementSnapshot(
            observed_at=datetime.now(UTC).isoformat(),
            engine_state=_text(diagnosis, "state") or "unknown",
            next_action=_text(diagnosis, "action") or "Run connection diagnosis.",
            version=_text(agent, "version"), runtime_id=_text(agent, "runtime_id"),
            instance_id=_text(agent, "instance_id"),
            capabilities={k: v for k, v in capabilities.items() if isinstance(v, bool)}
            if isinstance(capabilities, dict) else {},
            active_resources={k: v for k, v in resources.items()
                              if isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            if isinstance(resources, dict) else {},
            setup=self.setup.progress(), devices=devices, device_registry_state=registry_state,
            automatic_stable_updates=automatic_updates_enabled(self.directory),
        )
