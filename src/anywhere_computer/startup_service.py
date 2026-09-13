"""Explicit startup registration with an owned receipt and no overwrite path."""

import os
import secrets
import stat
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Literal, Self, cast

import psutil
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .autostart import Platform, StartupDefinition, StartupMode, current_definition
from .cloudflare_tunnel import TunnelCredential, cloudflared_executable
from .codex_context import _executable as codex_executable
from .http_service import load_http_config
from .locking import ProcessLock
from .owner_credentials import OwnerCredentials
from .startup_native import NativeStartup, StartupSnapshot
from .state import prepare_directory


class StartupRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    policy_version: Literal[1, 2] = 1
    platform: Literal["darwin", "linux", "win32"]
    user: str
    directory: str
    interpreter: str
    connector: str | None
    mode: StartupMode = "remote"
    startup_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    native_fingerprint: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    codex_executable: str | None = None
    utf8_python: bool = False  # Missing field preserves pre-UTF-8 startup definitions.
    isolated_python: bool = False  # Missing field identifies a pre-isolation receipt.

    @model_validator(mode="after")
    def check_connection_mode(self) -> Self:
        if self.mode == "remote" and self.connector is None:
            raise ValueError("Remote startup requires a connector")
        if self.mode == "local" and self.connector is not None:
            raise ValueError("Local startup cannot contain a connector")
        return self


def _read_file(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError("Startup files must not be symlinks")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Startup files must be regular files")
        if sys.platform != "win32" and info.st_uid != os.getuid():
            raise PermissionError("Startup file belongs to another user")
        content = source.read(131073)
    if len(content) > 131072:
        raise ValueError("Startup file exceeds the size limit")
    return content


def _create_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=".autostart-", dir=path.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        # Publish complete bytes atomically without replacing a racing owner's file.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _record(directory: Path) -> StartupRecord | None:
    raw = _read_file(directory / "autostart.json")
    if raw is None:
        return None
    try:
        record = StartupRecord.model_validate_json(raw)
    except ValueError:
        raise ValueError("Startup registration record is invalid; it was preserved") from None
    if (record.platform != sys.platform or record.directory != str(directory.resolve()) or
            record.user != psutil.Process().username()):
        raise ValueError("Startup registration belongs to a different user, platform or directory")
    return record


def _definition(directory: Path, record: StartupRecord) -> StartupDefinition:
    return current_definition(directory, connector=record.connector, startup_id=record.startup_id,
                              executable=record.interpreter, isolated_python=record.isolated_python,
                              codex_executable=record.codex_executable,
                              utf8_python=record.utf8_python,
                              policy_version=record.policy_version, mode=record.mode)


def _check_file(definition: StartupDefinition) -> bool:
    content = _read_file(definition.path)
    if content is None:
        return False
    if content != definition.content:
        raise ValueError("Startup definition was changed or belongs to another registration")
    return True


def _save_registered(directory: Path, record: StartupRecord, fingerprint: str) -> StartupRecord:
    destination = directory / "autostart.json"
    if _record(directory) != record:
        raise ValueError("Startup registration record changed during the operation")
    updated = record.model_copy(update={"native_fingerprint": fingerprint})
    descriptor, raw_path = tempfile.mkstemp(prefix=".autostart-", dir=directory)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(updated.model_dump_json(indent=2).encode())
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return updated


def _check_snapshot(record: StartupRecord, snapshot: StartupSnapshot) -> None:
    if snapshot.present and (not snapshot.matches or (
        record.native_fingerprint and record.native_fingerprint != snapshot.fingerprint
    )):
        raise ValueError("OS startup definition was changed or belongs to another registration")


def _result(
    definition: StartupDefinition, snapshot: StartupSnapshot, *, mode: StartupMode,
) -> dict[str, str | bool]:
    return {
        "state": "registered" if snapshot.present and snapshot.enabled else "not_enabled",
        "name": definition.name,
        "definition_path": str(definition.path),
        "native_running": snapshot.running,
        "public_reachability": "unverified",
        "mode": mode,
    }


def startup_status(directory: Path) -> dict[str, str | bool]:
    record = _record(directory)
    if record is None:
        return {"state": "not_installed", "native_state": "unverified", "changed": False}
    definition = _definition(directory, record)
    file_exists = _check_file(definition)
    snapshot = NativeStartup(definition).query()
    _check_snapshot(record, snapshot)
    return {**_result(definition, snapshot, mode=record.mode), "definition_exists": file_exists,
            "python_isolation_upgrade_required": not record.isolated_python,
            "persistence_upgrade_required": record.policy_version < 2,
            "receipt_confirmed": bool(record.native_fingerprint), "changed": False}


def install_startup(
    directory: Path, *, connector: str | None = None, mode: StartupMode | None = None,
) -> dict[str, str | bool]:
    directory = directory.resolve()
    prepare_directory(directory)
    with ProcessLock(directory / "autostart.lock"):
        return _install_startup_locked(directory, connector=connector, mode=mode)


def _install_startup_locked(
    directory: Path, *, connector: str | None = None, mode: StartupMode | None = None,
) -> dict[str, str | bool]:
    record = _record(directory)
    if record is not None and not record.isolated_python:
        raise ValueError(
            "Legacy startup must be removed with autostart-uninstall before "
            "autostart-install can register isolated Python; credentials are preserved"
        )
    if record is not None and connector is not None and connector != record.connector:
        raise ValueError("Startup connector differs; uninstall before changing it")
    selected_mode = mode or (record.mode if record is not None else "remote")
    if selected_mode not in {"remote", "local"}:
        raise ValueError("Unsupported startup mode")
    if record is not None and record.mode != selected_mode:
        raise ValueError("Startup mode differs; uninstall before changing it")
    selected = connector if record is None else record.connector
    executable = None
    if selected_mode == "remote":
        executable = os.path.abspath(cloudflared_executable(selected))
        config = load_http_config(directory)
        owner = OwnerCredentials(directory, resource=config.resource, owner=config.owner)
        owner.ensure_initialized()
        TunnelCredential(directory).read()
    elif selected is not None:
        raise ValueError("Local startup does not accept a tunnel connector")
    if record is None:
        try:
            pinned_codex = str(codex_executable(None))
        except FileNotFoundError:
            pinned_codex = None
        record = StartupRecord(
            platform=cast(Platform, sys.platform), user=psutil.Process().username(),
            directory=str(directory),
            interpreter=os.path.abspath(sys.executable), connector=executable,
            startup_id=secrets.token_hex(16),
            isolated_python=True, utf8_python=True,
            codex_executable=pinned_codex, policy_version=2, mode=selected_mode,
        )
        definition = _definition(directory, record)
        backend = NativeStartup(definition)
        if _read_file(definition.path) is not None or backend.query().present:
            raise ValueError("A startup definition already exists; it will not be overwritten")
        _create_file(directory / "autostart.json", record.model_dump_json(indent=2).encode())
    else:
        definition = _definition(directory, record)
        backend = NativeStartup(definition)
    if not Path(record.interpreter).is_file():
        raise ValueError("Registered Python interpreter is unavailable")
    snapshot = backend.query()
    _check_snapshot(record, snapshot)
    created_definition = not _check_file(definition)
    if created_definition:
        _create_file(definition.path, definition.content)

    def check_registered_snapshot(observed: StartupSnapshot) -> None:
        try:
            _check_snapshot(record, observed)
        except ValueError:
            # Remove only this attempt's still-identical pending login file.
            # Never stop or reload a conflicting native registration.
            if created_definition and not record.native_fingerprint and _check_file(definition):
                definition.path.unlink()
            raise

    snapshot = backend.query()
    check_registered_snapshot(snapshot)
    if not snapshot.present or not snapshot.enabled:
        try:
            backend.install()
        except (OSError, RuntimeError, TimeoutError):
            # Resolve a lost acknowledgment once. Never force or blindly repeat creation.
            snapshot = backend.query()
            check_registered_snapshot(snapshot)
            if not snapshot.present or not snapshot.enabled:
                raise RuntimeError(
                    "Startup registration was not confirmed; receipt preserved"
                ) from None
        else:
            snapshot = backend.query()
    check_registered_snapshot(snapshot)
    if not snapshot.present or not snapshot.enabled:
        raise RuntimeError("Startup registration was not confirmed; receipt preserved")
    _save_registered(directory, record, snapshot.fingerprint)
    return _result(definition, snapshot, mode=record.mode)


def _wait_stopped(directory: Path, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with ExitStack() as locks:
                for name in ("remote-watch.lock", "http-watch.lock", "http-server.lock",
                             "cloudflare-tunnel.lock", "persistent-watch.lock", "local-watch.lock"):
                    locks.enter_context(ProcessLock(directory / name))
                return
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Startup was disabled but process shutdown is not confirmed"
                ) from None
            time.sleep(0.1)


def uninstall_startup(
    directory: Path, *, expected_mode: StartupMode | None = None,
) -> dict[str, str | bool]:
    directory = directory.resolve()
    if not directory.exists():
        return {"state": "not_installed", "changed": False}
    with ProcessLock(directory / "autostart.lock"):
        if expected_mode is not None:
            record = _record(directory)
            if record is not None and record.mode != expected_mode:
                raise ValueError("Startup mode changed; inspect before removal")
        return _uninstall_startup_locked(directory)


def _uninstall_startup_locked(directory: Path) -> dict[str, str | bool]:
    record = _record(directory)
    if record is None:
        return {"state": "not_installed", "changed": False}
    definition = _definition(directory, record)
    _check_file(definition)
    backend = NativeStartup(definition)
    snapshot = backend.query()
    _check_snapshot(record, snapshot)
    if snapshot.present:
        backend.uninstall(snapshot)
    after = backend.query()
    _check_snapshot(record, after)
    # Native removal may acknowledge before the owned job disappears. Observe
    # completion without issuing another removal or accepting a replacement job.
    deadline = time.monotonic() + 5
    while (after.running or after.enabled or
           (after.present and definition.platform != "linux")):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
        after = backend.query()
        _check_snapshot(record, after)
    if after.running or after.enabled or (after.present and definition.platform != "linux"):
        raise RuntimeError("OS startup removal was not confirmed; receipt preserved")
    _wait_stopped(directory)
    if _record(directory) != record:
        raise ValueError("Startup receipt changed during removal")
    if _check_file(definition):
        definition.path.unlink()
    backend.files_removed()
    after = backend.query()
    if after.present or after.running or after.enabled:
        raise RuntimeError("OS startup removal was not confirmed; receipt preserved")
    (directory / "autostart.json").unlink()
    return {"state": "uninstalled", "changed": True, "credentials_preserved": True}


def start_startup(directory: Path) -> dict[str, str | bool]:
    """Start only a validated, enabled registration; never kill a running job."""
    directory = directory.resolve()
    with ProcessLock(directory / "autostart.lock"):
        record = _record(directory)
        if record is None:
            raise ValueError("Startup is not installed")
        definition = _definition(directory, record)
        if not _check_file(definition):
            raise ValueError("Startup definition is missing")
        backend = NativeStartup(definition)
        snapshot = backend.query()
        _check_snapshot(record, snapshot)
        if not snapshot.present or not snapshot.enabled:
            raise ValueError("Startup must be registered and enabled before starting")
        if not snapshot.running:
            backend.start(snapshot)
        after = backend.query()
        _check_snapshot(record, after)
        return _result(definition, after, mode=record.mode)


def upgrade_startup(directory: Path, *, connector: str | None = None) -> dict[str, str | bool]:
    """Serialize migration and retain a durable previous receipt until recovery completes."""
    directory = directory.resolve()
    prepare_directory(directory)
    with ProcessLock(directory / "autostart.lock"):
        backup = directory / "autostart-upgrade-previous.json"
        if _read_file(backup) is not None:
            raise ValueError("An interrupted startup upgrade requires recovery; receipt preserved")
        old = _record(directory)
        if old is None:
            return _install_startup_locked(directory, connector=connector)
        definition = _definition(directory, old)
        if not _check_file(definition):
            raise ValueError("Cannot upgrade a missing startup definition")
        _check_snapshot(old, NativeStartup(definition).query())
        _create_file(backup, old.model_dump_json(indent=2).encode())
        try:
            _uninstall_startup_locked(directory)
            result = _install_startup_locked(
                directory, connector=connector or old.connector, mode=old.mode,
            )
        except (OSError, RuntimeError, ValueError):
            try:
                current = _record(directory)
                if current != old:
                    if current is not None:
                        _uninstall_startup_locked(directory)
                    _create_file(directory / "autostart.json",
                                 old.model_dump_json(indent=2).encode())
                _install_startup_locked(directory)
            except (OSError, RuntimeError, ValueError):
                raise RuntimeError(
                    "Startup upgrade failed; previous receipt retained in "
                    "autostart-upgrade-previous.json for recovery"
                ) from None
            backup.unlink()
            raise RuntimeError("Startup upgrade failed; previous registration restored") from None
        backup.unlink()
        return result
