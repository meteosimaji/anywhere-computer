"""Prepare a shared engine state without switching clients or rewriting source stores."""

import hashlib
import re
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

from .downloads import Downloads
from .locking import ProcessLock
from .state import Ledger, prepare_directory
from .uploads import Uploads

DATABASES = {
    'operations.sqlite3': ('operation_results', 'operations', 'runtime_settings'),
    'uploads/uploads.sqlite3': ('uploads', 'chunks'),
    'downloads/downloads.sqlite3': ('downloads', 'chunks'),
}


def _normalize(directory: Path) -> None:
    ledger = Ledger(directory)
    try:
        ledger.connection.execute(
            'CREATE TABLE IF NOT EXISTS runtime_settings '
            '(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)',
        )
        for (identity,) in ledger.connection.execute('SELECT id FROM operations'):
            ledger.get(identity)  # Check stored result hashes before accepting the snapshot.
        Uploads(directory, file_locks=directory / 'file-locks')
        Downloads(directory)
    finally:
        ledger.close()


def _snapshot(source: Path, destination: Path, tables: tuple[str, ...]) -> None:
    if source.is_symlink():
        raise ValueError('Engine databases must not be symbolic links')
    if not source.exists():
        return
    prepare_directory(destination.parent)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as reader:
        present = {row[0] for row in reader.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
        if present - set(tables):
            raise ValueError('Unrecognized engine tables; source store preserved')
        if reader.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('Source database failed integrity validation')
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer)


def _merge_database(source: Path, target: Path, tables: tuple[str, ...]) -> None:
    with closing(sqlite3.connect(source)) as reader, closing(sqlite3.connect(target)) as writer:
        with writer:
            writer.execute('BEGIN IMMEDIATE')
            for table in tables:
                columns = reader.execute(f'PRAGMA table_info({table})').fetchall()
                if columns != writer.execute(f'PRAGMA table_info({table})').fetchall():
                    raise ValueError('Engine database schemas differ after normalization')
                keys = [index for index, column in enumerate(columns) if column[5]]
                if not keys:
                    raise ValueError('Migration requires an explicit primary key')
                predicate = ' AND '.join(f'"{columns[index][1]}"=?' for index in keys)
                placeholders = ','.join('?' for _ in columns)
                for row in reader.execute(f'SELECT * FROM {table}'):
                    existing = writer.execute(
                        f'SELECT * FROM {table} WHERE {predicate}',
                        tuple(row[index] for index in keys),
                    ).fetchone()
                    if existing is not None:
                        if existing != row:
                            raise ValueError(f'Conflicting {table} record; source stores preserved')
                    else:
                        writer.execute(f'INSERT INTO {table} VALUES ({placeholders})', row)


def backup_entries(source: Path) -> list[Path]:
    """Select the content-addressed store, leaving manual archives in the source."""
    if source.is_symlink():
        raise ValueError('Backup directory must not be a symbolic link')
    if not source.exists():
        return []
    entries = []
    for path in source.iterdir():
        if not re.fullmatch('[a-f0-9]{64}', path.name):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('Unexpected backup entry; source preserved')
        entries.append(path)
    return entries


def _copy_backups(source: Path, target: Path) -> None:
    entries = backup_entries(source)
    prepare_directory(target)
    for path in entries:
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != path.name:
                raise ValueError('Backup failed integrity validation')
        destination = target / path.name
        if destination.exists():
            with destination.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != path.name:
                    raise ValueError('Conflicting backup content')
        else:
            shutil.copyfile(path, destination)


def stage_shared_engine(
    local_directory: Path, http_directory: Path, destination: Path,
) -> None:
    """Offline preparation only. Keep both services stopped until activation completes.

    Reject running services through their existing process locks. The new directory
    is not a selected runtime; no credentials, endpoint or source database is changed.
    Runtime settings must agree if both sources contain explicit settings.
    """
    local_directory, http_directory = local_directory.resolve(), http_directory.resolve()
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Shared state destination must be unused')
    prepare_directory(destination.parent)
    with ExitStack() as locks:
        for path in sorted({local_directory / 'startup.lock', local_directory / 'agent.lock',
                            http_directory / 'http-server.lock',
                            destination.parent / (destination.name + '.migration.lock')}):
            locks.enter_context(ProcessLock(path, timeout=0))
        _stage_shared_engine_locked(local_directory, http_directory, destination)


def _stage_shared_engine_locked(
    local_directory: Path, http_directory: Path, destination: Path,
) -> None:
    sources = (local_directory, http_directory / 'http-server/engine')
    if not all((source / 'operations.sqlite3').is_file() for source in sources):
        raise ValueError('Both source engine ledgers must exist')
    with tempfile.TemporaryDirectory(prefix='.shared-state-', dir=destination.parent) as raw:
        temporary = Path(raw)
        combined = temporary / 'combined'
        _normalize(combined)
        for index, source in enumerate(sources):
            copied = temporary / str(index)
            for relative, tables in DATABASES.items():
                _snapshot(source / relative, copied / relative, tables)
            # Migrate only the private snapshots, including legacy five-column ledgers.
            _normalize(copied)
            for relative, tables in DATABASES.items():
                _merge_database(copied / relative, combined / relative, tables)
            _copy_backups(source / 'backups', combined / 'backups')
        if destination.exists() or destination.is_symlink():
            raise ValueError('Shared state destination appeared during preparation')
        combined.rename(destination)
