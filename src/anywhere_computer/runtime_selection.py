"""Persist the installed interpreter selected by an explicit engine update."""

import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .models import Contract


class RuntimeSelection(Contract):
    version: Literal[1] = 1
    executable: str = Field(min_length=1, max_length=4096)
    runtime_id: str = Field(pattern=r'^[a-f0-9]{64}$')

    @field_validator('executable')
    @classmethod
    def absolute_executable(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError('Selected interpreter must be absolute')
        return value


def _read_selection(path: Path) -> RuntimeSelection | None:
    if path.is_symlink():
        raise ValueError('Runtime selection must not be a symbolic link')
    try:
        with path.open('rb') as source:
            raw = source.read(8193)
    except FileNotFoundError:
        return None
    if len(raw) > 8192:
        raise ValueError('Runtime selection exceeds size limit')
    selection = RuntimeSelection.model_validate_json(raw)
    if not Path(selection.executable).is_file() or not os.access(selection.executable, os.X_OK):
        raise ValueError('Selected runtime is unavailable; restore its installation')
    return selection


def load_runtime_selection(
    directory: Path, *, recovery: RuntimeSelection | None = None,
) -> RuntimeSelection | None:
    pending = _read_selection(directory / 'runtime-update.pending.json')
    if pending is not None and pending != recovery:
        raise RuntimeError(
            'Runtime update is incomplete; resume start with its selected installation',
        )
    return _read_selection(directory / 'runtime-selection.json')


def _write_selection(destination: Path, selection: RuntimeSelection) -> None:
    directory = destination.parent
    if destination.is_symlink():
        raise RuntimeError('Runtime selection must not be a symbolic link')
    try:
        descriptor, name = tempfile.mkstemp(prefix='.runtime-selection-', dir=directory)
    except OSError as error:
        raise RuntimeError('Could not stage the selected runtime') from error
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            target.write(selection.model_dump_json())
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
        if os.name != 'nt':
            parent = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    except OSError as error:
        raise RuntimeError('Could not persist the selected runtime') from error
    finally:
        temporary.unlink(missing_ok=True)


def begin_runtime_update(directory: Path, selection: RuntimeSelection) -> None:
    """Caller holds startup.lock; persist intent before stopping or starting an engine."""
    destination = directory / 'runtime-update.pending.json'
    pending = _read_selection(destination)
    if pending is not None and pending != selection:
        raise RuntimeError('Another runtime update is incomplete')
    _write_selection(destination, selection)


def save_runtime_selection(directory: Path, selection: RuntimeSelection) -> None:
    """Caller holds startup.lock; selection is published only after readiness."""
    pending_path = directory / 'runtime-update.pending.json'
    pending = _read_selection(pending_path)
    if pending is not None and pending != selection:
        raise RuntimeError('Another runtime update is incomplete')
    _write_selection(directory / 'runtime-selection.json', selection)
    if pending is not None:
        cancel_runtime_update(directory, selection)


def cancel_runtime_update(directory: Path, selection: RuntimeSelection) -> None:
    """Clear only this intent after readiness or a confirmed refusal to stop."""
    path = directory / 'runtime-update.pending.json'
    if _read_selection(path) != selection:
        raise RuntimeError('Runtime update intent changed')
    try:
        path.unlink()
        if os.name != 'nt':
            parent = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    except OSError as error:
        raise RuntimeError('Could not finish the runtime update record') from error
