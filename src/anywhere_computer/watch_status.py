"""Bounded historical supervisor observations, never evidence of current liveness."""

import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .state import prepare_directory

WatchEvent = Literal[
    "child_started", "restart_wait", "restart_limit", "exited", "interrupted", "launch_error",
    "credential_check", "credentials_ready", "credential_store_unavailable", "credential_rejected",
]


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


def save_watch_observation(
    path: Path, event: WatchEvent, attempts: int, limit: int, exit_code: int | None,
    *, startup_attempt: int | None = None,
) -> None:
    observation = WatchObservation(event=event, updated_at=time.time(), supervisor_pid=os.getpid(),
                                   restart_attempts=attempts, restart_limit=limit,
                                   last_exit_code=exit_code, startup_attempt=startup_attempt)
    prepare_directory(path.parent)
    descriptor, raw = tempfile.mkstemp(prefix=".watch-status-", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(observation.model_dump_json().encode())
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
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
