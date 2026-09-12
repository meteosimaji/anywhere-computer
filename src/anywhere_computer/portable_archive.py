"""Inspect portable archive bytes without importing or executing bundled code."""

import hashlib
import json
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import Field, model_validator

from .engine_selection import engine_directory
from .models import Contract
from .runtime_identity import ENGINE_API_VERSION
from .state import require_ledger_compatibility

MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 20000
MANIFEST_LIMIT = 4 * 1024 * 1024
PREFIX = 'Anywhere Computer/'
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class PortableInspection:
    python_version: str
    source_commit: str
    platform: str
    file_count: int
    expanded_bytes: int
    engine_api: 'CompatibilityRange'
    ledger_schema: 'LedgerCompatibility'


class CompatibilityRange(Contract):
    minimum: int = Field(strict=True, ge=0)
    maximum: int = Field(strict=True, ge=0)

    @model_validator(mode='after')
    def ordered(self) -> 'CompatibilityRange':
        if self.minimum > self.maximum:
            raise ValueError('Invalid compatibility range')
        return self


class LedgerCompatibility(CompatibilityRange):
    writes: int = Field(strict=True, ge=0)

    @model_validator(mode='after')
    def readable_writes(self) -> 'LedgerCompatibility':
        if not self.minimum <= self.writes <= self.maximum:
            raise ValueError('Runtime cannot read its own written ledger schema')
        return self


def require_portable_compatibility(inspection: PortableInspection, control: Path) -> int:
    """Read current selected ledger before applying a candidate; never restore older data."""
    if not inspection.engine_api.minimum <= ENGINE_API_VERSION <= inspection.engine_api.maximum:
        raise ValueError('Candidate engine API is incompatible with this connector')
    current = require_ledger_compatibility(
        engine_directory(control), minimum=inspection.ledger_schema.minimum,
        maximum=inspection.ledger_schema.maximum,
    )
    if not inspection.ledger_schema.minimum <= current <= inspection.ledger_schema.maximum:
        raise ValueError('Candidate cannot initialize the current ledger schema')
    if inspection.ledger_schema.writes < current:
        raise ValueError('Candidate would downgrade the ledger schema')
    return current


def _member_name(name: str) -> str:
    if not name.startswith(PREFIX):
        raise ValueError('Archive member is outside the application directory')
    relative = name[len(PREFIX):]
    parts = relative.split('/')
    if (not relative or '\\' in relative or ':' in relative or '\x00' in relative
            or any(part in {'', '.', '..'} or part.endswith((' ', '.')) for part in parts)
            or PurePosixPath(relative).is_absolute()):
        raise ValueError('Invalid portable archive path')
    if any(re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])', part.split('.')[0])
           for part in parts):
        raise ValueError('Reserved portable archive path')
    return relative


def inspect_portable(archive_path: Path, *, expected_sha256: str) -> PortableInspection:
    if not re.fullmatch('[a-f0-9]{64}', expected_sha256):
        raise ValueError('Invalid expected archive digest')
    with archive_path.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != expected_sha256:
            raise ValueError('Portable archive checksum mismatch')
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if not 1 <= len(members) <= MAX_MEMBERS:
                raise ValueError('Portable archive member limit exceeded')
            names: dict[str, zipfile.ZipInfo] = {}
            folded = set()
            expanded = 0
            for member in members:
                name = _member_name(member.filename)
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if member.is_dir() or kind not in {0, stat.S_IFREG} or member.flag_bits & 1:
                    raise ValueError('Unsupported portable member type')
                if name.casefold() in folded:
                    raise ValueError('Duplicate portable archive path')
                folded.add(name.casefold())
                names[name] = member
                expanded += member.file_size
                if expanded > MAX_EXPANDED_BYTES:
                    raise ValueError('Portable archive expanded size limit exceeded')
            manifest_entry = names.get('manifest.json')
            if manifest_entry is None or manifest_entry.file_size > MANIFEST_LIMIT:
                raise ValueError('Portable manifest missing or too large')
            manifest = json.loads(archive.read(manifest_entry))
            files = manifest.get('files') if isinstance(manifest, dict) else None
            release = manifest.get('release') if isinstance(manifest, dict) else None
            if not isinstance(files, dict) or set(names) != set(files) | {'manifest.json'}:
                raise ValueError('Portable manifest does not enumerate exactly the archive files')
            if 'manifest.json' in files or not isinstance(release, dict):
                raise ValueError('Invalid portable manifest')
            for name, digest in files.items():
                if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                    raise ValueError('Invalid portable file digest')
                with archive.open(names[name]) as member_file:
                    calculated = hashlib.sha256()
                    for block in iter(lambda: member_file.read(1024 * 1024), b''):
                        calculated.update(block)
                    if calculated.hexdigest() != digest:
                        raise ValueError('Portable member checksum mismatch')
            version, commit = release.get('python_version'), release.get('source_commit')
            platform = manifest.get('platform')
            if (not isinstance(version, str) or not 1 <= len(version) <= 64
                    or not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit)
                    or release.get('source_dirty') is not False
                    or not isinstance(platform, str) or not 1 <= len(platform) <= 128):
                raise ValueError('Invalid or dirty portable provenance')
            api = CompatibilityRange.model_validate(release.get('engine_api'))
            ledger = LedgerCompatibility.model_validate(release.get('ledger_schema'))
            return PortableInspection(version, commit, platform, len(files), expanded, api, ledger)


def stage_portable(archive_path: Path, parent: Path, *, expected_sha256: str) -> Path:
    """Return a new, unselected application directory; never execute its contents.

    The private snapshot prevents a changed source pathname from substituting
    different bytes between validation and extraction. Publisher verification
    must supply the expected digest; this function does not establish provenance.
    """
    parent = parent.resolve(strict=True)
    stage = Path(tempfile.mkdtemp(prefix='.portable-stage-', dir=parent))
    try:
        snapshot = stage / 'archive.zip'
        with archive_path.open('rb') as source, snapshot.open('xb') as snapshot_file:
            size = 0
            for block in iter(lambda: source.read(1024 * 1024), b''):
                size += len(block)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError('Portable archive compressed size limit exceeded')
                snapshot_file.write(block)
        inspect_portable(snapshot, expected_sha256=expected_sha256)
        app = stage / 'Anywhere Computer'
        app.mkdir(mode=0o700)
        with zipfile.ZipFile(snapshot) as archive:
            for member in archive.infolist():
                target = app / _member_name(member.filename)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(member) as source, target.open('xb') as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                # Keep executable bits, but discard setuid/setgid and group/world writes.
                target.chmod(0o700 if member.external_attr >> 16 & 0o111 else 0o600)
        snapshot.unlink()
        return app
    except BaseException:
        shutil.rmtree(stage)
        raise
