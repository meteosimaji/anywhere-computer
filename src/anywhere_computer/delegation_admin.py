"""Local, password-authenticated owner management of delegated HTTP grants."""

import time
import uuid
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from .delegated_routes import DelegatedRouteStore
from .delegated_tasks import DelegatedTaskGrant, DelegatedTaskStore
from .devices import DeviceStore
from .http_service import _check_enrollment, _http_authority
from .owner_credentials import OwnerCredentials


def _canonical_roots(values: tuple[str, ...]) -> tuple[str, ...]:
    roots: list[str] = []
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.exists() or not path.is_dir():
            raise ValueError("Delegated roots must be existing absolute directories")
        resolved = path.resolve(strict=True)
        if path != resolved:
            raise ValueError("Delegated roots must not contain symbolic links")
        roots.append(str(resolved))
    return tuple(roots)


def manage_delegation(
    directory: Path, *, action: str, password: str,
    parent_grant_id: str | None = None, child_id: str | None = None,
    device_id: str = "local", tools: frozenset[str] = frozenset(),
    read_roots: tuple[str, ...] = (), write_roots: tuple[str, ...] = (),
    expires_in: int = 3600,
    target_bearer: str | None = None,
    target_child_id: str | None = None,
    credentials: OwnerCredentials | None = None,
) -> dict[str, JsonValue]:
    """Execute a trusted local owner action; never put the bearer in logs."""
    with _http_authority(directory) as (config, authority):
        _check_enrollment(authority, config)
        credentials = credentials or OwnerCredentials(
            directory, resource=config.resource, owner=config.owner,
        )
        if credentials.resource != config.resource or credentials.owner != config.owner:
            raise ValueError("Owner credential identity does not match HTTP configuration")
        if not credentials.verify(password):
            raise ValueError("Owner authentication failed")
        delegated = DelegatedTaskStore(
            directory / "http-server" / "delegated-tasks", authority,
        )
        try:
            if action in {'route', 'unroute'}:
                if child_id is None:
                    raise ValueError('Select a delegated child for the route')
                child = next((grant for grant, _ in delegated.list_grants(owner=config.owner)
                              if grant.child_id == child_id), None)
                if child is None or child.device_id == 'local':
                    raise ValueError('Remote delegated child is unavailable')
                if action == 'route' and delegated.current(child_id) is None:
                    raise ValueError('Remote delegated child is no longer active')
                device_directory = (Path(config.shared_agent_directory)
                                    if config.shared_agent_directory is not None else directory)
                devices = DeviceStore(device_directory, read_only=True)
                try:
                    device = devices.get(child.device_id)
                finally:
                    devices.close()
                resource = (device['resource'] if device['transport'] == 'http'
                            else 'ssh:' + str(device['ssh_host']))
                if not isinstance(resource, str):
                    raise ValueError('Delegated route requires a valid target')
                routes = DelegatedRouteStore(device_directory / 'delegated-routes')
                try:
                    if action == 'route':
                        if target_bearer is None or target_child_id is None:
                            raise ValueError('Target child bearer and ID are required')
                        routes.bind(child_id, child.device_id, resource, target_bearer,
                                    target_child_id=target_child_id)
                    else:
                        routes.revoke(child_id, child.device_id)
                    route_target_id = routes.target_child_id(child_id, child.device_id)
                finally:
                    routes.close()
                return {'child_id': child_id, 'device_id': child.device_id,
                        'target_route': 'bound' if action == 'route' else 'revoked',
                        'target_child_id': route_target_id}
            if action == "list":
                parent_ids = [str(row[0]) for row in authority.db.execute(
                    "SELECT id FROM grants WHERE owner=? AND device=? AND client=? "
                    "ORDER BY rowid DESC", (config.owner, config.device, config.client),
                )]
                parents: list[JsonValue] = []
                for grant_id in parent_ids:
                    parent = authority.current_grant(grant_id)
                    if parent is not None:
                        parents.append({
                            "grant_id": grant_id,
                            "tools": cast(JsonValue, sorted(parent.tools)),
                        })
                owned_children = delegated.list_grants(owner=config.owner)
                remote_children = [grant for grant, _ in owned_children
                                   if grant.device_id != 'local']
                list_routes: DelegatedRouteStore | None = None
                if remote_children:
                    device_directory = (Path(config.shared_agent_directory)
                                        if config.shared_agent_directory is not None else
                                        directory)
                    list_routes = DelegatedRouteStore(device_directory / 'delegated-routes')
                try:
                    children: list[JsonValue] = [
                        {"child_id": grant.child_id,
                         "parent_grant_id": grant.parent_grant_id,
                         "device_id": grant.device_id,
                         "tools": cast(JsonValue, sorted(grant.tools)),
                         "read_roots": list(grant.read_roots),
                         "write_roots": list(grant.write_roots),
                         "expires_at": grant.expires_at, "active": active,
                         "target_child_id": (list_routes.target_child_id(
                             grant.child_id, grant.device_id)
                             if list_routes is not None and grant.device_id != 'local'
                             else None)}
                        for grant, active in owned_children
                    ]
                finally:
                    if list_routes is not None:
                        list_routes.close()
                return {"parents": parents, "children": children}
            if action == "revoke":
                child = next((grant for grant, _ in delegated.list_grants(owner=config.owner)
                              if grant.child_id == child_id), None)
                if child is None:
                    raise ValueError("Delegated child was not found for this owner")
                delegated.revoke(child.child_id)
                recorded_target: str | None = None
                if child.device_id != 'local':
                    device_directory = (Path(config.shared_agent_directory)
                                        if config.shared_agent_directory is not None
                                        else directory)
                    routes = DelegatedRouteStore(device_directory / 'delegated-routes')
                    try:
                        recorded_target = routes.target_child_id(child.child_id,
                                                                 child.device_id)
                        routes.revoke(child.child_id, child.device_id)
                    finally:
                        routes.close()
                return {"child_id": child_id, "revoked": True,
                        "source_child": "revoked",
                        "source_route": ("revoked" if child.device_id != 'local'
                                         else "not_applicable"),
                        "target_child_grant": ("not_revoked_here"
                                               if child.device_id != 'local'
                                               else "not_applicable"),
                        "target_child_id": recorded_target}
            if action != "issue" or parent_grant_id is None:
                raise ValueError("Invalid delegation administration action")
            parent = authority.current_grant(parent_grant_id)
            if (parent is None or parent.owner != config.owner or parent.device != config.device
                    or parent.client != config.client):
                raise ValueError("Parent grant is unavailable for this HTTP connection")
            if not 1 <= expires_in <= 86400:
                raise ValueError("Delegated lifetime must be 1–86400 seconds")
            new_id = uuid.uuid4().hex
            grant = DelegatedTaskGrant(
                owner=config.owner, child_id=new_id, parent_grant_id=parent_grant_id,
                device_id=device_id, tools=tools,
                read_roots=_canonical_roots(read_roots),
                write_roots=_canonical_roots(write_roots),
                expires_at=time.time() + expires_in,
            )
            bearer = delegated.issue(grant)
            return {"child_id": new_id, "bearer": bearer,
                    "expires_at": grant.expires_at,
                    "resource": config.resource}
        finally:
            delegated.close()
