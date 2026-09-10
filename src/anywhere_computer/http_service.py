"""Persistent, explicitly configured HTTP service behind a same-host HTTPS proxy."""

import asyncio
import json
import os
import secrets
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from .authorization import AuthorizationStore, validate_authorization_url
from .authorized_http import AuthorizedDeviceMCP
from .browser_authorization import BrowserAuthorization
from .engine import Engine
from .files import read_bytes
from .http_mcp import HTTPMCP
from .locking import ProcessLock
from .oauth_endpoints import OAuthEndpoints
from .owner_credentials import OwnerCredentials
from .state import prepare_directory


class HTTPServiceConfig(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    resource: str
    owner: str = Field(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
    device: str = Field(pattern=r"^[a-f0-9]{32}$")
    client: str = Field(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
    port: int = Field(ge=1, le=65535)
    scopes: frozenset[str] = Field(min_length=1, max_length=64)
    redirects: frozenset[str] = Field(min_length=1, max_length=10)

    @field_serializer("scopes", "redirects", when_used="json")
    def ordered_values(self, values: frozenset[str]) -> list[str]:
        return sorted(values)

    @field_validator("resource")
    @classmethod
    def check_resource(cls, value: str) -> str:
        validate_authorization_url(value)
        parsed = urlsplit(value)
        if parsed.path != "/mcp" or parsed.query:
            raise ValueError("HTTP service requires a canonical HTTPS /mcp resource")
        return value

    @field_validator("redirects")
    @classmethod
    def check_redirects(cls, values: frozenset[str]) -> frozenset[str]:
        for value in values:
            validate_authorization_url(value, loopback=True)
        return values


def load_http_config(directory: Path) -> HTTPServiceConfig:
    path = directory / "http-server" / "config.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("HTTP configuration must not be a symbolic link")
    try:
        raw = read_bytes(path)
        if len(raw) > 16384:
            raise ValueError("HTTP configuration exceeds limit")
        return HTTPServiceConfig.model_validate_json(raw)
    except (OSError, ValueError):
        raise ValueError(
            "HTTP service configuration is missing or invalid; run http-configure"
        ) from None


async def configure_http(
    directory: Path,
    *,
    resource: str,
    owner: str,
    client: str,
    port: int,
    scopes: frozenset[str],
    redirects: frozenset[str],
) -> HTTPServiceConfig:
    config = HTTPServiceConfig(
        resource=resource,
        owner=owner,
        device=secrets.token_hex(16),
        client=client,
        port=port,
        scopes=scopes,
        redirects=redirects,
    )
    return await save_http_config(directory, config)


async def save_http_config(directory: Path, config: HTTPServiceConfig) -> HTTPServiceConfig:
    """Commit an already validated setup plan without changing its reviewed fields."""
    prepare_directory(directory)
    with ProcessLock(directory / "http-server.lock"):
        destination = directory / "http-server"
        if destination.exists() or destination.is_symlink():
            raise ValueError(
                "HTTP service is already configured; existing authorization is preserved"
            )
        # Publish config and enrolled authorization together. A failed setup leaves
        # no visible partially configured service and never replaces an existing one.
        with tempfile.TemporaryDirectory(prefix=".http-setup-", dir=directory) as temporary:
            staged = Path(temporary)
            engine = Engine(staged / "engine")
            try:
                store = AuthorizationStore(
                    staged / "authorization",
                    resource=config.resource,
                    known_tools=frozenset(engine.tools),
                )
                try:
                    store.register_client(config.client, config.redirects)
                    store.enroll_device(config.owner, config.device, config.scopes)
                finally:
                    store.close()
            finally:
                await engine.close()
            path = staged / "config.json"
            with path.open("x", encoding="utf-8") as output:
                output.write(config.model_dump_json(indent=2) + "\n")
                output.flush()
                os.fsync(output.fileno())
            staged.rename(destination)
    return config


@dataclass(frozen=True)
class RunningHTTPService:
    config: HTTPServiceConfig
    adapter: HTTPMCP


def _check_enrollment(store: AuthorizationStore, config: HTTPServiceConfig) -> None:
    client = store.db.execute(
        "SELECT redirects FROM clients WHERE id=?", (config.client,)
    ).fetchone()
    device = store.db.execute(
        "SELECT owner,tools FROM authorized_devices WHERE id=?", (config.device,)
    ).fetchone()
    if (
        client is None
        or device is None
        or device[0] != config.owner
        or frozenset(json.loads(client[0])) != config.redirects
        or frozenset(json.loads(device[1])) != config.scopes
        or config.scopes - store.known_tools
    ):
        raise ValueError(
            "HTTP configuration differs from its enrolled authorization; refusing to start"
        )


@asynccontextmanager
async def http_service(
    directory: Path,
    *,
    credentials: OwnerCredentials | None = None,
) -> AsyncIterator[RunningHTTPService]:
    prepare_directory(directory)
    with ProcessLock(directory / "http-server.lock"):
        config = load_http_config(directory)
        owner = credentials or OwnerCredentials(
            directory, resource=config.resource, owner=config.owner
        )
        if (
            owner.owner != config.owner
            or owner.resource != config.resource
            or owner.directory != directory.resolve()
        ):
            raise ValueError("Owner credentials do not match the configured service")
        await asyncio.to_thread(owner.ensure_initialized)
        service_directory = directory / "http-server"
        database = service_directory / "authorization" / "authorization.sqlite3"
        if not database.is_file() or database.is_symlink():
            raise ValueError("HTTP authorization database is missing; refusing to recreate it")
        engine = Engine(service_directory / "engine", file_locks=directory / "file-locks")
        try:
            store = AuthorizationStore(
                service_directory / "authorization",
                resource=config.resource,
                known_tools=frozenset(engine.tools),
            )
            try:
                _check_enrollment(store, config)
                backend = AuthorizedDeviceMCP(
                    store,
                    engine,
                    owner=config.owner,
                    device=config.device,
                    client=config.client,
                    allowed_tools=config.scopes,
                )
                consent = BrowserAuthorization(store, owner, device=config.device)
                oauth = OAuthEndpoints(
                    store, authorization_endpoint=consent.authorization_endpoint,
                    authorization_response_iss_supported=True,
                )
                adapter = HTTPMCP(
                    backend.authenticate,
                    backend.session,
                    origins=frozenset({consent.origin}),
                    public_routes={**oauth.routes(), **consent.routes()},
                    auth_challenge=oauth.challenge,
                )
                try:
                    await adapter.start(config.port)
                    yield RunningHTTPService(config, adapter)
                finally:
                    await adapter.close()
            finally:
                store.close()
        finally:
            await engine.close()


@contextmanager
def _http_authority(directory: Path) -> Iterator[tuple[HTTPServiceConfig, AuthorizationStore]]:
    config = load_http_config(directory)
    authority_directory = directory / "http-server" / "authorization"
    database = authority_directory / "authorization.sqlite3"
    if not database.is_file() or database.is_symlink() or authority_directory.is_symlink():
        raise ValueError("HTTP authorization database is missing or is a symbolic link")
    store = AuthorizationStore(
        authority_directory, resource=config.resource, known_tools=config.scopes
    )
    try:
        yield config, store
    finally:
        store.close()


def revoke_http_device(directory: Path) -> None:
    """Trusted administration; usable while serving, and durable across restarts."""
    with _http_authority(directory) as (config, store):
        store.revoke_device(owner=config.owner, device=config.device)


def enable_http_device(directory: Path) -> bool:
    """Allow fresh consent after revocation; never restore existing credentials."""
    with _http_authority(directory) as (config, store):
        _check_enrollment(store, config)
        return store.enable_device(owner=config.owner, device=config.device)


def retain_http_grants(directory: Path) -> int:
    """Explicit local approval to retain this configured client's active grants."""
    with _http_authority(directory) as (config, store):
        _check_enrollment(store, config)
        return store.retain_active_grants(
            owner=config.owner, device=config.device, client=config.client,
        )


def http_authorization_status(directory: Path) -> dict[str, str | bool]:
    """Report persisted enrollment state, not network reachability or client login."""
    with _http_authority(directory) as (config, store):
        _check_enrollment(store, config)
        return {
            "device_id": config.device,
            "device_enabled": store.device_enabled(owner=config.owner, device=config.device),
        }


async def serve_http(directory: Path) -> None:
    async with http_service(directory) as running:
        print(
            json.dumps(
                {
                    "http_listening": f"http://127.0.0.1:{running.config.port}",
                    "resource": running.config.resource,
                    "public_reachability": "unverified",
                }
            ),
            flush=True,
        )
        await asyncio.Event().wait()
