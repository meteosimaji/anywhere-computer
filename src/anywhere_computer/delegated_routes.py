"""Owner-provisioned child credentials for an exact remote HTTP device.

Only route metadata is stored in SQLite. The target child bearer lives in the
native OS credential store and is never copied to tool arguments or prompts.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .client_tokens import CredentialVault, validate_client_profile
from .credentials import SERVICE, secure_backend
from .locking import ProcessLock
from .ssh_transport import validate_ssh_host
from .state import prepare_directory


class DelegatedRouteStore:
    def __init__(self, directory: Path, *, vault: CredentialVault | None = None) -> None:
        prepare_directory(directory)
        self.directory = directory.resolve()
        self.vault = vault or secure_backend()
        database = directory / 'delegated-routes.sqlite3'
        if database.is_symlink():
            raise ValueError('Delegated route database must not be a symbolic link')
        self.db = sqlite3.connect(database, timeout=10)
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS delegated_routes ('
                            'child_id TEXT NOT NULL, device_id TEXT NOT NULL, '
                            'resource TEXT NOT NULL, active INTEGER NOT NULL, '
                            'target_child_id TEXT, '
                            'PRIMARY KEY(child_id, device_id))')
            if 'target_child_id' not in {
                row[1] for row in self.db.execute('PRAGMA table_info(delegated_routes)')
            }:
                self.db.execute('ALTER TABLE delegated_routes ADD COLUMN target_child_id TEXT')

    def _account(self, child_id: str, device_id: str) -> str:
        identity = f'{self.directory}:{child_id}:{device_id}'.encode()
        return 'delegated-route-' + hashlib.sha256(identity).hexdigest()

    @staticmethod
    def _validate(child_id: str, device_id: str, resource: str) -> None:
        if (len(child_id) != 32 or len(device_id) != 32
                or any(char not in '0123456789abcdef' for char in child_id + device_id)):
            raise ValueError('Invalid delegated route identity')
        if resource.startswith('ssh:'):
            validate_ssh_host(resource.removeprefix('ssh:'))
        else:
            validate_client_profile(resource, 'delegated-child', 'route')

    def bind(self, child_id: str, device_id: str, resource: str, bearer: str,
             *, target_child_id: str | None = None) -> None:
        self._validate(child_id, device_id, resource)
        if target_child_id is not None and (
            len(target_child_id) != 32
            or any(char not in '0123456789abcdef' for char in target_child_id)
        ):
            raise ValueError('Invalid target delegated child ID')
        if (not isinstance(bearer, str) or not 1 <= len(bearer) <= 256
                or any(not 33 <= ord(char) <= 126 for char in bearer)):
            raise ValueError('Invalid target child bearer')
        with ProcessLock(self.directory / 'delegated-routes.lock', timeout=10):
            if self.db.execute('SELECT 1 FROM delegated_routes WHERE child_id=? '
                               'AND device_id=?', (child_id, device_id)).fetchone():
                raise ValueError('Delegated route already exists; issue a new child ID')
            account = self._account(child_id, device_id)
            try:
                self.vault.set_password(SERVICE, account, bearer)
            except Exception:
                raise ValueError('OS credential store could not save the route') from None
            try:
                with self.db:
                    self.db.execute('INSERT INTO delegated_routes '
                                    '(child_id,device_id,resource,active,target_child_id) '
                                    'VALUES (?,?,?,1,?)',
                                    (child_id, device_id, resource, target_child_id))
            except sqlite3.Error:
                try:
                    self.vault.delete_password(SERVICE, account)
                except Exception:
                    pass  # An orphaned secret cannot authorize a route without metadata.
                raise ValueError('Delegated route metadata could not be saved') from None

    def target_child_id(self, child_id: str, device_id: str) -> str | None:
        """Return the owner's recorded target ID, including for a revoked route."""
        with ProcessLock(self.directory / 'delegated-routes.lock', timeout=10):
            row = self.db.execute('SELECT target_child_id FROM delegated_routes '
                                  'WHERE child_id=? AND device_id=?',
                                  (child_id, device_id)).fetchone()
            return str(row[0]) if row is not None and row[0] is not None else None

    def bearer(self, child_id: str, device_id: str, resource: str) -> str:
        self._validate(child_id, device_id, resource)
        with ProcessLock(self.directory / 'delegated-routes.lock', timeout=10):
            row = self.db.execute('SELECT resource, active FROM delegated_routes '
                                  'WHERE child_id=? AND device_id=?',
                                  (child_id, device_id)).fetchone()
            if row is None or row[0] != resource or not row[1]:
                raise ValueError('Target child route is unavailable')
            try:
                bearer = self.vault.get_password(SERVICE, self._account(child_id, device_id))
            except Exception:
                raise ValueError('OS credential store could not read the route') from None
            if bearer is None:
                raise ValueError('Target child credential is unavailable')
            return bearer

    def revoke(self, child_id: str, device_id: str) -> None:
        with ProcessLock(self.directory / 'delegated-routes.lock', timeout=10):
            with self.db:
                changed = self.db.execute('UPDATE delegated_routes SET active=0 '
                                          'WHERE child_id=? AND device_id=?',
                                          (child_id, device_id))
            account = self._account(child_id, device_id)
            if changed.rowcount == 1:
                try:
                    if self.vault.get_password(SERVICE, account) is not None:
                        self.vault.delete_password(SERVICE, account)
                except Exception:
                    raise ValueError('OS credential store could not remove the route') from None

    def close(self) -> None:
        self.db.close()
