"""Offline, resumable selection of a shared store for both existing entrances."""

import hashlib
import os
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

from .engine_selection import EngineSelection, engine_directory
from .http_service import HTTPServiceConfig, load_http_config
from .locking import ProcessLock
from .models import Contract
from .state import prepare_directory
from .state_migration import DATABASES, _stage_shared_engine_locked, backup_entries

PENDING = 'engine-migration.pending.json'


class Migration(Contract):
    version: Literal[1] = 1
    local_directory: str
    http_directory: str
    destination: str
    http_before: str
    source_digest: str
    destination_digest: str


def _read(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError('Migration control files must not be symbolic links')
    try:
        with path.open('rb') as source:
            raw = source.read(32769)
    except FileNotFoundError:
        return None
    if len(raw) > 32768:
        raise ValueError('Migration control file exceeds size limit')
    return raw


def _sync(directory: Path) -> None:
    if os.name != 'nt':
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write(path: Path, content: bytes, expected: bytes | None) -> None:
    if _read(path) != expected:
        raise ValueError('Migration control file changed; existing state preserved')
    descriptor, name = tempfile.mkstemp(prefix='.engine-control-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        if expected is None:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(*directories: Path) -> str:
    digest = hashlib.sha256()
    for directory in directories:
        names = {relative + suffix for relative in DATABASES for suffix in ('', '-wal')}
        backups = directory / 'backups'
        names.update('backups/' + path.name for path in backup_entries(backups))
        digest.update(str(directory).encode() + b'\0')
        for name in sorted(names):
            path = directory / name
            if path.is_symlink():
                raise ValueError('Migration data must not be a symbolic link')
            digest.update(name.encode() + b'\0')
            if path.exists():
                with path.open('rb') as source:
                    digest.update(hashlib.file_digest(source, 'sha256').digest())
            else:
                digest.update(b'absent')
    return digest.hexdigest()


def migrate_engine(local_directory: Path, http_directory: Path) -> dict[str, str | bool]:
    """Run again with the same directories after interruption; services must be stopped.

    The pending records block updated local and HTTP services until both selections
    are committed. Source stores and existing credentials are never restored or deleted.
    """
    local, http = local_directory.resolve(), http_directory.resolve()
    with ExitStack() as locks:
        lock_paths = {local / 'startup.lock', local / 'agent.lock', http / 'http-server.lock'}
        for path in sorted(lock_paths):
            locks.enter_context(ProcessLock(path, timeout=0))
        config_path = http / 'http-server/config.json'
        config = load_http_config(http)
        pending_path = local / PENDING
        existing = _read(pending_path)
        selection_path = local / 'engine-selection.json'
        if existing is None:
            if _read(selection_path) is not None:
                selected = engine_directory(local)
                if config.shared_agent_directory != str(local):
                    raise ValueError('Existing engine selection does not match HTTP configuration')
                return {'state': 'selected', 'engine_directory': str(selected), 'changed': False}
            if config.shared_agent_directory is not None or _read(http / PENDING) is not None:
                raise ValueError('An existing shared configuration requires its original migration')
            root = local / 'engines'
            prepare_directory(root)
            destination = root / uuid.uuid4().hex
            before = _read(config_path)
            if before is None:
                raise ValueError('HTTP configuration disappeared')
            _stage_shared_engine_locked(local, http, destination)
            record = Migration(
                local_directory=str(local), http_directory=str(http),
                destination=str(destination), http_before=before.decode('utf-8'),
                source_digest=_digest(local, http / 'http-server/engine'),
                destination_digest=_digest(destination),
            )
            existing = record.model_dump_json().encode()
            _write(pending_path, existing, None)
        else:
            record = Migration.model_validate_json(existing)
            if record.local_directory != str(local) or record.http_directory != str(http):
                raise ValueError('Migration belongs to different control directories')
            destination = Path(record.destination)
            if destination.parent != local / 'engines' or not destination.is_dir():
                raise ValueError('Prepared migration destination is unavailable')
        http_pending = http / PENDING
        marker = _read(http_pending)
        if marker != existing:
            if marker is not None:
                raise ValueError('Another HTTP migration is pending')
            _write(http_pending, existing, None)
        if record.source_digest != _digest(local, http / 'http-server/engine'):
            raise ValueError('Source data changed after preparation; migration remains paused')
        if record.destination_digest != _digest(destination):
            raise ValueError('Prepared data changed; migration remains paused')
        updated = HTTPServiceConfig.model_validate_json(record.http_before).model_copy(
            update={'shared_agent_directory': str(local)},
        ).model_dump_json(indent=2).encode()
        current = _read(config_path)
        if current != updated:
            _write(config_path, updated, record.http_before.encode())
        selection = EngineSelection(directory=str(destination)).model_dump_json().encode()
        current = _read(selection_path)
        if current != selection:
            _write(selection_path, selection, None)
        # HTTP now routes through local, whose pending marker remains the final gate.
        for path in dict.fromkeys((http_pending, pending_path)):
            if _read(path) != existing:
                raise ValueError('Pending migration record changed before completion')
            path.unlink()
            _sync(path.parent)
        return {'state': 'selected', 'engine_directory': str(destination), 'changed': True,
                'credentials_preserved': True, 'restart_required': True}
