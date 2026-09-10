"""Bounded historical supervisor observations, never evidence of current liveness."""

import json
import os
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .runtime_identity import runtime_identity
from .state import prepare_directory

WatchEvent = Literal[
    "child_started", "restart_wait", "restart_limit", "exited", "interrupted", "launch_error",
    "credential_check", "credentials_ready", "credential_store_unavailable", "credential_rejected",
    "configuration_error", "credential_backend_error", "connector_check_error", "startup_conflict",
    "connector_starting", "connector_cleanup_error",
    "cooldown", "blocked", "unexpected_child_exit",
]
_GENERATION = secrets.token_hex(16)
_GENERATION_STARTED = time.monotonic()
_PROCESS_CREATED = psutil.Process().create_time()
_RUNTIME = runtime_identity()
_EVENT_SEQUENCE = 0


def lifecycle_print(value: object) -> None:
    """Optional console diagnostics must not own the service lifetime."""
    try:
        print(json.dumps(value), flush=True)
    except OSError:
        pass


WatchFailureKind = Literal["child_exit", "connector_error"]


class WatchObservation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    event: WatchEvent
    updated_at: float = Field(ge=0, allow_inf_nan=False)
    supervisor_pid: int = Field(gt=0)
    restart_attempts: int = Field(ge=0, le=5)
    restart_limit: int = Field(ge=0, le=5)
    last_exit_code: int | None = None
    startup_attempt: int | None = Field(default=None, ge=1, le=6)
    last_failure_kind: WatchFailureKind | None = None
    generation: str | None = None
    elapsed_seconds: float | None = None
    process_created_at: float | None = None
    runtime_id: str | None = None


def save_watch_observation(
    path: Path, event: WatchEvent, attempts: int, limit: int, exit_code: int | None,
    *, startup_attempt: int | None = None,
    failure_kind: WatchFailureKind | None = None,
) -> None:
    global _EVENT_SEQUENCE
    observation = WatchObservation(event=event, updated_at=time.time(), supervisor_pid=os.getpid(),
                                   restart_attempts=attempts, restart_limit=limit,
                                   last_exit_code=exit_code, startup_attempt=startup_attempt,
                                   last_failure_kind=failure_kind, generation=_GENERATION,
                                   elapsed_seconds=time.monotonic() - _GENERATION_STARTED,
                                   process_created_at=_PROCESS_CREATED, runtime_id=_RUNTIME)
    prepare_directory(path.parent)
    descriptor, raw = tempfile.mkstemp(prefix=".watch-status-", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(observation.model_dump_json().encode())
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        # Fixed ring of atomic snapshots: no provider output or credentials.
        slot = _EVENT_SEQUENCE % 64
        _EVENT_SEQUENCE += 1
        history = path.with_name(path.stem + f".event-{slot:02d}.json")
        descriptor, history_raw = tempfile.mkstemp(prefix=".watch-event-", dir=path.parent)
        history_temp = Path(history_raw)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(observation.model_dump_json().encode())
            os.replace(history_temp, history)
        finally:
            history_temp.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


def read_watch_observation(path: Path) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"state": "unavailable", "current_process_state": "unverified"}
    try:
        if path.is_symlink():
            raise ValueError("Symlink observation")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Nonregular observation")
            get_uid = getattr(os, "getuid", None)
            if get_uid is not None and info.st_uid != get_uid():
                raise ValueError("Foreign observation")
            raw = source.read(8193)
        if len(raw) > 8192:
            raise ValueError("Oversized observation")
        observation = WatchObservation.model_validate_json(raw)
    except FileNotFoundError:
        return result
    except (OSError, ValueError):
        result["state"] = "unreadable"
        return result
    result.update(state="recorded", last_observation=observation.model_dump(mode="json"))
    return result
