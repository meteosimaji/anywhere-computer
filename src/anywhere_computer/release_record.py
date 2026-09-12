"""Persist the prepared installation before an update can change the running engine."""

import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .github_releases import ReleaseCandidate
from .models import Contract


class PreparedRelease(Contract):
    version: Literal[1] = 1
    candidate: ReleaseCandidate
    installation: str = Field(min_length=1, max_length=4096)
    state: Literal['prepared', 'applied'] = 'prepared'

    @field_validator('installation')
    @classmethod
    def absolute_installation(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError('Prepared installation must be absolute')
        return value


def read_prepared_release(control: Path) -> PreparedRelease | None:
    path = control / 'prepared-release.json'
    if path.is_symlink():
        raise ValueError('Prepared release record must not be a symbolic link')
    try:
        with path.open('rb') as source:
            raw = source.read(16385)
    except FileNotFoundError:
        return None
    if len(raw) > 16384:
        raise ValueError('Prepared release record exceeds size limit')
    return PreparedRelease.model_validate_json(raw)


def save_prepared_release(control: Path, record: PreparedRelease) -> None:
    """Caller holds release-install.lock; a failed save must prevent activation."""
    path = control / 'prepared-release.json'
    if path.is_symlink():
        raise ValueError('Prepared release record must not be a symbolic link')
    descriptor, name = tempfile.mkstemp(prefix='.prepared-release-', dir=control)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            target.write(record.model_dump_json())
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(control, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
