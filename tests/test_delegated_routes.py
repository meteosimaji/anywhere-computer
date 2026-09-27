"""Remote child credentials stay outside device metadata and cannot cross routes."""

import sqlite3
import threading

import pytest
from test_client_tokens import MemoryVault

from anywhere_computer.delegated_routes import DelegatedRouteStore

RESOURCE = 'https://target.example/mcp'
CHILD = 'a' * 32
DEVICE = 'b' * 32


def test_route_is_bound_to_child_device_resource_and_native_vault(tmp_path):
    vault = MemoryVault()
    route = DelegatedRouteStore(tmp_path, vault=vault)
    try:
        route.bind(CHILD, DEVICE, RESOURCE, 'target-child-secret',
                   target_child_id='c' * 32)
        assert route.target_child_id(CHILD, DEVICE) == 'c' * 32
        assert route.bearer(CHILD, DEVICE, RESOURCE) == 'target-child-secret'
        with pytest.raises(ValueError, match='unavailable'):
            route.bearer('c' * 32, DEVICE, RESOURCE)
        with pytest.raises(ValueError, match='unavailable'):
            route.bearer(CHILD, 'd' * 32, RESOURCE)
        with pytest.raises(ValueError, match='unavailable'):
            route.bearer(CHILD, DEVICE, 'https://other.example/mcp')
        with pytest.raises(ValueError, match='already exists'):
            route.bind(CHILD, DEVICE, RESOURCE, 'replacement-secret')
        assert 'target-child-secret' not in (
            tmp_path / 'delegated-routes.sqlite3').read_bytes().decode('latin-1')
    finally:
        route.close()
    reopened = DelegatedRouteStore(tmp_path, vault=vault)
    try:
        assert reopened.target_child_id(CHILD, DEVICE) == 'c' * 32
        assert reopened.bearer(CHILD, DEVICE, RESOURCE) == 'target-child-secret'
        reopened.revoke(CHILD, DEVICE)
        assert reopened.target_child_id(CHILD, DEVICE) == 'c' * 32
        with pytest.raises(ValueError, match='unavailable'):
            reopened.bearer(CHILD, DEVICE, RESOURCE)
        with sqlite3.connect(tmp_path / 'delegated-routes.sqlite3') as connection:
            assert connection.execute('SELECT active FROM delegated_routes').fetchone() == (0,)
    finally:
        reopened.close()


def test_route_keyring_failure_never_echoes_target_bearer(tmp_path):
    vault = MemoryVault()
    vault.fail_write = 1
    route = DelegatedRouteStore(tmp_path, vault=vault)
    try:
        with pytest.raises(ValueError, match='credential store') as caught:
            route.bind(CHILD, DEVICE, RESOURCE, 'do-not-print-this-bearer')
        assert 'do-not-print-this-bearer' not in str(caught.value)
        assert route.db.execute('SELECT COUNT(*) FROM delegated_routes').fetchone() == (0,)
    finally:
        route.close()


def test_legacy_route_migration_preserves_bearer_with_unknown_target_id(tmp_path):
    vault = MemoryVault()
    route = DelegatedRouteStore(tmp_path, vault=vault)
    try:
        route.bind(CHILD, DEVICE, RESOURCE, 'old-target-bearer')
        with route.db:
            route.db.execute('ALTER TABLE delegated_routes DROP COLUMN target_child_id')
    finally:
        route.close()
    migrated = DelegatedRouteStore(tmp_path, vault=vault)
    try:
        assert migrated.target_child_id(CHILD, DEVICE) is None
        assert migrated.bearer(CHILD, DEVICE, RESOURCE) == 'old-target-bearer'
    finally:
        migrated.close()


def test_route_rejects_invalid_target_child_id_before_storing_bearer(tmp_path):
    vault = MemoryVault()
    route = DelegatedRouteStore(tmp_path, vault=vault)
    try:
        with pytest.raises(ValueError, match='target delegated child ID'):
            route.bind(CHILD, DEVICE, RESOURCE, 'secret', target_child_id='wrong')
        assert route.db.execute('SELECT count(*) FROM delegated_routes').fetchone()[0] == 0
        assert vault.data == {}
    finally:
        route.close()


def test_ssh_route_is_bound_to_verified_host_alias(tmp_path):
    route = DelegatedRouteStore(tmp_path, vault=MemoryVault())
    try:
        route.bind(CHILD, DEVICE, 'ssh:trusted-target', 'target-child-secret')
        assert route.bearer(CHILD, DEVICE, 'ssh:trusted-target') == 'target-child-secret'
        with pytest.raises(ValueError, match='unavailable'):
            route.bearer(CHILD, DEVICE, 'ssh:different-target')
        with pytest.raises(ValueError, match='host alias'):
            route.bearer(CHILD, DEVICE, 'ssh:-oProxyCommand=evil')
    finally:
        route.close()


def test_route_revoke_waits_for_inflight_bearer_read(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingVault(MemoryVault):
        def get_password(self, service, account):
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError('Synthetic vault wait expired')
            return super().get_password(service, account)

    vault = BlockingVault()
    route = DelegatedRouteStore(tmp_path, vault=vault)
    route.bind(CHILD, DEVICE, RESOURCE, 'target-child-secret')
    route.close()
    observed = []
    revoke_started = threading.Event()
    def read_bearer():
        local = DelegatedRouteStore(tmp_path, vault=vault)
        try:
            observed.append(local.bearer(CHILD, DEVICE, RESOURCE))
        finally:
            local.close()

    def revoke_route():
        revoke_started.set()
        local = DelegatedRouteStore(tmp_path, vault=vault)
        try:
            local.revoke(CHILD, DEVICE)
        finally:
            local.close()

    read = threading.Thread(target=read_bearer)
    revoke = threading.Thread(target=revoke_route)
    try:
        read.start()
        assert entered.wait(timeout=2)
        revoke.start()
        assert revoke_started.wait(timeout=2)
        assert revoke.is_alive()
        release.set()
        read.join(timeout=2)
        revoke.join(timeout=2)
        assert not read.is_alive() and not revoke.is_alive()
        assert observed == ['target-child-secret']
        current = DelegatedRouteStore(tmp_path, vault=vault)
        try:
            with pytest.raises(ValueError, match='unavailable'):
                current.bearer(CHILD, DEVICE, RESOURCE)
        finally:
            current.close()
    finally:
        release.set()
        read.join(timeout=2)
        if revoke_started.is_set():
            revoke.join(timeout=2)
