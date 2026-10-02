"""Durable revocation intent independent of an in-flight file guard's SQLite lock."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

RevocationState = Literal['pending', 'revoked']
GrantKind = Literal['parent', 'child']


def _queue(database: Path) -> Path:
    path = database.with_name(database.stem + '-revocations.sqlite3')
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError('Grant revocation storage is unavailable')
    return path


def _initialize(db: sqlite3.Connection) -> None:
    db.execute('CREATE TABLE IF NOT EXISTS revocations ('
               'grant_id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL '
               "CHECK(state IN ('pending','revoked')))")
    db.execute('CREATE TABLE IF NOT EXISTS pending_audit ('
               'operation_id TEXT NOT NULL, child_id TEXT NOT NULL, tool TEXT NOT NULL, '
               'decision TEXT NOT NULL, reason TEXT NOT NULL, observed REAL NOT NULL, '
               'PRIMARY KEY(child_id,operation_id))')
    db.execute('CREATE TABLE IF NOT EXISTS operation_inputs ('
               'child_id TEXT NOT NULL, operation_id TEXT NOT NULL, request_digest TEXT NOT NULL, '
               'PRIMARY KEY(child_id,operation_id))')


def bind_request_input(database: Path, child_id: str, operation_id: str, digest: str) -> None:
    """Bind new inputs without waiting on an admitted descriptor-confined file operation."""
    with closing(sqlite3.connect(_queue(database), timeout=10)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        _initialize(db)
        db.execute('INSERT OR IGNORE INTO operation_inputs VALUES (?,?,?)',
                   (child_id, operation_id, digest))
        row = db.execute('SELECT request_digest FROM operation_inputs '
                         'WHERE child_id=? AND operation_id=?',
                         (child_id, operation_id)).fetchone()
        if row != (digest,):
            raise ValueError('Delegated operation ID was used for another request')


def _saved(database: Path, grant_id: str) -> tuple[str, RevocationState] | None:
    path = _queue(database)
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT owner,state FROM revocations WHERE grant_id=?',
                         (grant_id,)).fetchone()
    if row is None:
        return None
    if not isinstance(row[0], str) or row[1] not in {'pending', 'revoked'}:
        raise ValueError('Invalid saved grant revocation')
    return row[0], row[1]


def revocation_requested(database: Path, grant_id: str) -> bool:
    """A committed intent denies new dispatch, including while its UPDATE is pending."""
    return _saved(database, grant_id) is not None


def request_revocation(database: Path, grant_id: str, owner: str) -> None:
    """Caller must first verify this grant's owner in its authorization store."""
    with closing(sqlite3.connect(_queue(database), timeout=10)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        _initialize(db)
        db.execute('INSERT OR IGNORE INTO revocations VALUES (?,?,?)',
                   (grant_id, owner, 'pending'))
        row = db.execute('SELECT owner FROM revocations WHERE grant_id=?',
                         (grant_id,)).fetchone()
        if row != (owner,):
            raise ValueError('Grant revocation belongs to another owner')


def finish_revocation(database: Path, grant_id: str, kind: GrantKind
                      ) -> RevocationState | None:
    """Try the original UPDATE without waiting behind an admitted file worker.

    A failed authorization-store write retains the committed intent and pending
    state. Only a committed UPDATE may become revoked. Queue failures propagate;
    they never return confirmation that the durable intent was saved.
    """
    saved = _saved(database, grant_id)
    if saved is None:
        return None
    owner, state = saved
    if state == 'revoked':
        return state
    try:
        with closing(sqlite3.connect(database.absolute().as_uri() + '?mode=rw',
                                     uri=True, timeout=0)) as db, db:
            if kind == 'parent':
                row = db.execute('SELECT owner FROM grants WHERE id=?',
                                 (grant_id,)).fetchone()
                if row != (owner,):
                    raise ValueError('Grant revocation owner is unavailable')
                db.execute('UPDATE grants SET revoked=1 WHERE id=? AND owner=?',
                           (grant_id, owner))
            else:
                row = db.execute('SELECT body FROM grants WHERE child_id=?',
                                 (grant_id,)).fetchone()
                if row is None or json.loads(row[0]).get('owner') != owner:
                    raise ValueError('Grant revocation owner is unavailable')
                db.execute('UPDATE grants SET revoked=1 WHERE child_id=?', (grant_id,))
    except (sqlite3.Error, OSError, ValueError):
        # The independent intent remains the admission gate. This is pending,
        # never confirmation that a running operation stopped or the UPDATE won.
        return 'pending'
    with closing(sqlite3.connect(_queue(database), timeout=10)) as db, db:
        db.execute("UPDATE revocations SET state='revoked' WHERE grant_id=? AND owner=?",
                   (grant_id, owner))
    return 'revoked'


def record_pending_audit(database: Path, operation_id: str, child_id: str,
                         tool: str, reason: str, observed: float) -> None:
    """Save a denial without waiting on a file worker's grant database reservation."""
    with closing(sqlite3.connect(_queue(database), timeout=10)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        _initialize(db)
        db.execute('INSERT OR IGNORE INTO pending_audit VALUES (?,?,?,?,?,?)',
                   (operation_id, child_id, tool, 'denied', reason, observed))


def flush_pending_audit(database: Path) -> None:
    """Bounded, idempotent reconciliation; keep queued rows until main commit."""
    path = _queue(database)
    if not path.exists():
        return
    with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)) as queue:
        rows = queue.execute('SELECT * FROM pending_audit ORDER BY rowid LIMIT 100').fetchall()
    if not rows:
        return
    try:
        with closing(sqlite3.connect(database.absolute().as_uri() + '?mode=rw',
                                     uri=True, timeout=0)) as db, db:
            db.executemany('INSERT OR IGNORE INTO audit VALUES (?,?,?,?,?,?)', rows)
    except sqlite3.Error:
        return  # The denials remain durably queued, without claiming main commit.
    with closing(sqlite3.connect(path, timeout=10)) as queue, queue:
        queue.executemany('DELETE FROM pending_audit WHERE operation_id=? AND child_id=?',
                          [(row[0], row[1]) for row in rows])
