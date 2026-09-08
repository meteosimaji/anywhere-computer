"""Opt-in public metadata checks owned by the running remote service."""

import asyncio
import json
import math
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .http_service import load_http_config
from .locking import ProcessLock
from .state import prepare_directory


class PublicMonitorSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    enabled: bool = False
    resource: str = Field(default="", max_length=2048)


class PublicMonitorObservation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    resource: str = Field(max_length=2048)
    updated_at: float = Field(ge=0, allow_inf_nan=False)
    loopback_state: Literal[
        "metadata_reachable", "unreachable", "unexpected_response", "resource_mismatch",
        "configuration_unavailable",
    ]
    public_state: Literal[
        "metadata_reachable", "unreachable", "unexpected_response", "resource_mismatch",
        "certificate_verification_failed", "not_requested",
    ]
    consecutive_failures: int = Field(ge=0, le=2147483647)


def _read_monitor_file[MonitorModel: BaseModel](
    path: Path, model: type[MonitorModel],
) -> MonitorModel | None:
    if path.is_symlink():
        raise ValueError("Monitor files must not be symlinks")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        get_uid = getattr(os, "getuid", None)
        if not stat.S_ISREG(info.st_mode) or (get_uid is not None and info.st_uid != get_uid()):
            raise ValueError("Monitor file is not an owned regular file")
        raw = source.read(8193)
    if len(raw) > 8192:
        raise ValueError("Monitor file exceeds the limit")
    return model.model_validate_json(raw)


def _write_monitor_file(path: Path, model: BaseModel) -> None:
    prepare_directory(path.parent)
    descriptor, raw = tempfile.mkstemp(prefix=".remote-health-", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(model.model_dump_json().encode())
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure_public_monitor(directory: Path, *, enabled: bool) -> dict[str, JsonValue]:
    prepare_directory(directory)
    with ProcessLock(directory / "remote-health-control.lock"):
        settings = PublicMonitorSettings(
            enabled=enabled, resource=load_http_config(directory).resource if enabled else "",
        )
        destination = directory / "remote-health.json"
        previous = _read_monitor_file(destination, PublicMonitorSettings)
        changed = previous != settings
        if changed:
            _write_monitor_file(destination, settings)
    return {"enabled": enabled, "resource": settings.resource, "changed": changed,
            "interval_seconds": 30, "service_started": False}


def public_monitor_status(directory: Path) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"settings_state": "disabled", "history_state": "unavailable",
                                    "current_connectivity": "unverified", "authenticated": False}
    try:
        settings = _read_monitor_file(directory / "remote-health.json", PublicMonitorSettings)
        if settings is not None and settings.enabled:
            result["settings_state"] = (
                "enabled" if settings.resource == load_http_config(directory).resource
                else "configuration_changed"
            )
    except (OSError, ValueError):
        result["settings_state"] = "unreadable"
    try:
        observation = _read_monitor_file(
            directory / "remote-health-observation.json", PublicMonitorObservation,
        )
        if observation is not None:
            result.update(history_state="recorded",
                          last_observation=observation.model_dump(mode="json"))
    except (OSError, ValueError):
        result["history_state"] = "unreadable"
    return result


async def monitor_public_health(directory: Path, *, interval: float = 30) -> None:
    # Deferred import: diagnostics also displays the independently stored history.
    from .http_diagnostics import diagnose_remote

    if not math.isfinite(interval) or interval < 0.01:
        raise ValueError("Invalid public monitor interval")
    failures = 0
    selected_resource = ""
    while True:
        try:
            settings = _read_monitor_file(directory / "remote-health.json", PublicMonitorSettings)
            if settings is None or not settings.enabled:
                failures, selected_resource = 0, ""
            elif settings.resource == load_http_config(directory).resource:
                if selected_resource != settings.resource:
                    failures, selected_resource = 0, settings.resource
                report = await asyncio.wait_for(
                    diagnose_remote(directory, probe_public=True,
                                    expected_resource=settings.resource), 10,
                )
                local, public = report["loopback"], report["public"]
                if not isinstance(local, dict) or not isinstance(public, dict):
                    raise ValueError("Invalid metadata diagnosis")
                healthy = local.get("state") == public.get("state") == "metadata_reachable"
                failures = 0 if healthy else min(failures + 1, 2147483647)
                observation = PublicMonitorObservation.model_validate({
                    "resource": settings.resource, "updated_at": time.time(),
                    "loopback_state": local["state"], "public_state": public["state"],
                    "consecutive_failures": failures,
                })
                # A disable or changed endpoint invalidates an in-flight result.
                if (_read_monitor_file(directory / "remote-health.json", PublicMonitorSettings)
                        == settings and load_http_config(directory).resource == settings.resource):
                    _write_monitor_file(directory / "remote-health-observation.json", observation)
        except (OSError, ValueError, TimeoutError):
            print(json.dumps({"public_monitor_check_recorded": False}), flush=True)
        await asyncio.sleep(interval)
