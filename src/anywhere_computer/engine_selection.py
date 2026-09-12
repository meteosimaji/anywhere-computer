"""Resolve engine data while keeping endpoint metadata and credentials in place."""

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .models import Contract


class EngineSelection(Contract):
    version: Literal[1] = 1
    directory: str = Field(min_length=1, max_length=4096)

    @field_validator('directory')
    @classmethod
    def absolute_directory(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError('Selected engine directory must be absolute')
        return value


def require_no_migration(control: Path) -> None:
    pending = control / 'engine-migration.pending.json'
    if pending.exists() or pending.is_symlink():
        raise RuntimeError('Engine migration is incomplete; recover it before starting services')


def engine_directory(control: Path) -> Path:
    require_no_migration(control)
    path = control / 'engine-selection.json'
    if path.is_symlink():
        raise ValueError('Engine selection must not be a symbolic link')
    try:
        with path.open('rb') as source:
            raw = source.read(8193)
    except FileNotFoundError:
        return control
    if len(raw) > 8192:
        raise ValueError('Engine selection exceeds size limit')
    selection = EngineSelection.model_validate_json(raw)
    selected = Path(selection.directory)
    if selected.is_symlink() or not selected.is_dir():
        raise ValueError('Selected engine directory is missing or is a symbolic link')
    ledger = selected / 'operations.sqlite3'
    if ledger.is_symlink() or not ledger.is_file():
        raise ValueError(
            'Selected engine ledger is missing; refusing to create an empty replacement',
        )
    return selected
