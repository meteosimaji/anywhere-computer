"""Persistent, explicitly configured HTTP service behind a same-host HTTPS proxy."""

import asyncio
import json
import os
import secrets
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .authorization import AuthorizationStore, validate_authorization_url
from .authorized_http import AuthorizedDeviceMCP
from .browser_authorization import BrowserAuthorization
from .connection import ensure_agent, exchange
from .delegated_tasks import DelegatedTaskStore
from .device_router import ROUTER_TOOLS
from .engine import Engine
from .engine_selection import require_no_migration
from .files import read_bytes
from .http_mcp import HTTPMCP
from .http_tool_profile import HTTPToolProfile, validate_http_tool_profile
from .locking import ProcessLock
from .models import MAX_TOOL_SCOPES
from .oauth_endpoints import OAuthEndpoints
from .owner_credentials import OwnerCredentials
from .owner_passkeys import OwnerPasskeys
from .state import prepare_directory
from .subchat_device_save import SUBCHAT_SAVE_TOOLS
from .subchat_gateway import (
    SUBCHAT_AUTH_SCOPES,
    SubchatGatewayConfig,
    lazy_subchat_gateway,
    subchat_ledger_owner,
)


class HTTPServiceConfig(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    resource: str
    owner: str = Field(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
    device: str = Field(pattern=r"^[a-f0-9]{32}$")
    client: str = Field(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
    port: int = Field(ge=1, le=65535)
    scopes: frozenset[str] = Field(min_length=1, max_length=MAX_TOOL_SCOPES)
    redirects: frozenset[str] = Field(min_length=1, max_length=10)
    shared_agent_directory: str | None = Field(default=None, max_length=4096)
    subchat: SubchatGatewayConfig | None = None
    tool_profile: HTTPToolProfile = "full"

    @model_validator(mode="after")
    def check_subchat_selection(self) -> "HTTPServiceConfig":
        validate_http_tool_profile(self.tool_profile, self.scopes,
                                   has_subchat=self.subchat is not None)
        if self.subchat is None and self.scopes & (SUBCHAT_AUTH_SCOPES | SUBCHAT_SAVE_TOOLS):
            raise ValueError("Subchat scopes require an explicit gateway selection")
        return self

    @field_validator("shared_agent_directory")
    @classmethod
    def check_agent_directory(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).is_absolute():
            raise ValueError("Shared agent directory must be absolute")
        return value

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
    subchat: SubchatGatewayConfig | None = None,
    tool_profile: HTTPToolProfile = "full",
) -> HTTPServiceConfig:
    config = HTTPServiceConfig(
        resource=resource,
        owner=owner,
        device=secrets.token_hex(16),
        client=client,
        port=port,
        scopes=scopes,
        redirects=redirects,
        subchat=subchat,
        tool_profile=tool_profile,
    )
    return await save_http_config(directory, config)


async def save_http_config(directory: Path, config: HTTPServiceConfig) -> HTTPServiceConfig:
    """Commit an already validated setup plan without changing its reviewed fields."""
    # Frozen models can still be copied/constructed without validation by trusted
    # callers. Revalidate before touching enrollment or publishing configuration.
    config = HTTPServiceConfig.model_validate_json(config.model_dump_json())
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
                    known_tools=(frozenset(engine.tools) | ROUTER_TOOLS
                                 | ((SUBCHAT_AUTH_SCOPES | SUBCHAT_SAVE_TOOLS)
                                    if config.subchat else frozenset())),
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
    agent_directory: Path | None = None,
) -> AsyncIterator[RunningHTTPService]:
    prepare_directory(directory)
    with ProcessLock(directory / "http-server.lock"):
        require_no_migration(directory)
        config = load_http_config(directory)
        if agent_directory is None and config.shared_agent_directory is not None:
            agent_directory = Path(config.shared_agent_directory)
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
        # Existing installations retain their ledger until an explicit migration.
        # The launcher can select the shared local agent without creating another Engine.
        engine = None
        if agent_directory is None:
            engine = Engine(service_directory / "engine", file_locks=directory / "file-locks")
            known_tools = (frozenset(engine.tools) | ROUTER_TOOLS
                           | ((SUBCHAT_AUTH_SCOPES | SUBCHAT_SAVE_TOOLS)
                              if config.subchat else frozenset()))
        else:
            await asyncio.to_thread(ensure_agent, agent_directory)
            catalog = await exchange(agent_directory, "__catalog")
            entries = catalog.data.get("tools")
            if catalog.state != "completed" or not isinstance(entries, list):
                raise ValueError("Shared agent catalog is invalid")
            names: set[str] = set()
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(name := entry.get("name"), str):
                    raise ValueError("Shared agent catalog is invalid")
                names.add(name)
            known_tools = (frozenset(names) | ROUTER_TOOLS
                           | ((SUBCHAT_AUTH_SCOPES | SUBCHAT_SAVE_TOOLS)
                              if config.subchat else frozenset()))
        try:
            store = AuthorizationStore(
                service_directory / "authorization",
                resource=config.resource,
                known_tools=known_tools,
            )
            try:
                _check_enrollment(store, config)
                async with AsyncExitStack() as resources:
                    delegated = DelegatedTaskStore(service_directory / "delegated-tasks", store)
                    resources.callback(delegated.close)
                    selected_subchat = config.subchat

                    def queue_grant_active(grant_id: str, ledger_owner: str) -> bool:
                        grant = store.current_grant(grant_id)
                        return (selected_subchat is not None and grant is not None
                                and grant.owner == config.owner
                                and grant.device == config.device
                                and grant.client == config.client
                                and grant.resource == config.resource
                                and 'subchat_queue_auto' in grant.tools
                                and subchat_ledger_owner(
                                    grant, selected_subchat.account_id) == ledger_owner)

                    subchat_gateway = (await resources.enter_async_context(
                        lazy_subchat_gateway(
                            selected_subchat, owner=config.owner,
                            queue_grant_active=queue_grant_active))
                        if selected_subchat is not None else None)
                    backend = AuthorizedDeviceMCP(
                        store,
                        engine,
                        agent_directory=agent_directory,
                        owner=config.owner,
                        device=config.device,
                        client=config.client,
                        allowed_tools=config.scopes,
                        tool_profile=config.tool_profile,
                        device_directory=agent_directory or directory,
                        subchat_gateway=subchat_gateway,
                        delegated_tasks=delegated,
                    )
                    consent = BrowserAuthorization(store, owner, device=config.device)
                    oauth = OAuthEndpoints(
                        store, authorization_endpoint=consent.authorization_endpoint,
                        authorization_response_iss_supported=True,
                        advertised_scopes=config.scopes,
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
                        await backend.close()
                        await adapter.close()
            finally:
                store.close()
        finally:
            if engine is not None:
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


def begin_http_owner_passkey(directory: Path) -> str:
    """Begin local first-owner setup without a password or an existing grant.

    Only the trusted local CLI calls this. The public server can redeem the
    resulting one-use ticket but cannot issue one.
    """
    with _http_authority(directory) as (config, store):
        _check_enrollment(store, config)
        owner = OwnerCredentials(directory, resource=config.resource, owner=config.owner)
        if owner.is_initialized():
            if not owner.is_passkey_only():
                raise ValueError("Owner already uses a password; first-owner setup is unavailable")
            if (store.db.execute("SELECT 1 FROM grants WHERE revoked=0 LIMIT 1").fetchone()
                    is not None or (
                        store.db.execute("SELECT 1 FROM grants LIMIT 1").fetchone() is not None
                        and store.device_enabled(owner=config.owner, device=config.device)
                    )):
                raise ValueError("Existing grants require an offline owner reset")
        else:
            if store.db.execute("SELECT 1 FROM grants LIMIT 1").fetchone() is not None:
                raise ValueError("Existing grants require an offline owner reset")
            owner.initialize_passkey_only()
        return OwnerPasskeys(owner, device=config.device).issue_initial_local_ticket()


def enable_http_device(directory: Path) -> bool:
    """Allow fresh consent after revocation; never restore existing credentials."""
    with ProcessLock(directory / "owner-reset.lock"):
        with _http_authority(directory) as (config, store):
            _check_enrollment(store, config)
            return store.enable_device(owner=config.owner, device=config.device)


def _reset_http_owner(directory: Path, replacement: str | None) -> None:
    """Recover the configured owner only after all serving engines have stopped."""
    prepare_directory(directory)
    with ProcessLock(directory / "http-server.lock"):
        with ProcessLock(directory / "owner-reset.lock"):
            with _http_authority(directory) as (config, store):
                _check_enrollment(store, config)
                with ExitStack() as stopped_engines:
                    if config.shared_agent_directory is not None:
                        # A shared agent can retain terminal/process work after the
                        # HTTP server stops. Its lock is held through engine.close().
                        stopped_engines.enter_context(ProcessLock(
                            Path(config.shared_agent_directory) / "agent.lock"
                        ))
                    credentials = OwnerCredentials(
                        directory, resource=config.resource, owner=config.owner
                    )

                    def revoke_and_check() -> None:
                        store.begin_owner_reset(owner=config.owner, device=config.device)
                        if store.device_enabled(owner=config.owner, device=config.device):
                            raise ValueError("Device revocation was not confirmed")
                        remaining = store.db.execute(
                            "SELECT 1 FROM grants WHERE device=? AND revoked=0 LIMIT 1",
                            (config.device,),
                        ).fetchone()
                        if remaining is not None:
                            raise ValueError("Grant revocation was not confirmed")

                    if replacement is None:
                        credentials.reset_passkey_only(revoke_and_check)
                    else:
                        credentials.reset_password(replacement, revoke_and_check)
                    OwnerPasskeys(credentials, device=config.device).clear()
                    store.complete_owner_reset(owner=config.owner, device=config.device)


def reset_http_owner_password(directory: Path, replacement: str) -> None:
    _reset_http_owner(directory, replacement)


def reset_http_owner_passkey(directory: Path) -> None:
    """Offline passkey-only recovery; all old grants and keys are revoked."""
    _reset_http_owner(directory, None)


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
