"""Opt-in, service-owned ordinary Chat controller for direct HTTPS MCP."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager, closing
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from .authorization import GrantIdentity
from .models import Contract, OperationId, Reply, Request
from .subchat_mcp import (
    ReadOnlyHTTPCatalog,
    SubchatSession,
    capability_report,
    direct_gateway_catalog,
)
from .subchat_state import SubchatAccountMismatch, SubchatOperationNotFound, SubchatSubmissions

if TYPE_CHECKING:
    import httpx
    from playwright.async_api import BrowserContext

SUBCHAT_GATEWAY_TOOLS = frozenset({
    "subchat_capabilities", "subchat_catalog", "subchat_send", "subchat_message",
    "subchat_recover", "subchat_wait", "subchat_status",
})
_IDLE_CLOSE_SECONDS = 15.0
logger = logging.getLogger(__name__)


def subchat_ledger_owner(grant: GrantIdentity, account_id: str) -> str:
    """Stable, isolated owner for new submissions across fresh OAuth consents."""
    scope = json.dumps((grant.owner, grant.device, grant.client, grant.resource, account_id),
                       separators=(",", ":"), ensure_ascii=False)
    return "http-subchat:" + hashlib.sha256(scope.encode("utf-8")).hexdigest()


class SubchatGatewayConfig(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid",
                              hide_input_in_errors=True)
    profile: str = Field(min_length=1, max_length=4096)
    ledger: str = Field(min_length=1, max_length=4096)
    account_id: str = Field(min_length=1, max_length=256)
    consent: Literal["ordinary-chat-browser-control-approved"]

    @field_validator("profile", "ledger")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("Subchat profile and ledger paths must be absolute")
        return value


class SubchatGateway:
    """One controller per service lifetime; HTTP sessions are lightweight grant views."""

    def __init__(self, core_factory: Callable[[str], SubchatSession], *, owner: str,
                 account_id: str = "") -> None:
        self.core_factory = core_factory
        self.owner = owner
        self.account_id = account_id
        self.cores: dict[str, SubchatSession] = {}
        self.pending: dict[tuple[str, str], tuple[str, str, asyncio.Task[Reply]]] = {}

    def _core(self, grant_id: str) -> SubchatSession:
        core = self.cores.get(grant_id)
        if core is None:
            core = self.core_factory(grant_id)
            self.cores[grant_id] = core
        return core

    async def catalog(self, grant_id: str, granted: frozenset[str]) -> list[JsonValue]:
        result: list[JsonValue] = []
        for item in await self._core(grant_id).catalog():
            if (not isinstance(item, dict)
                    or item.get("name") not in granted & SUBCHAT_GATEWAY_TOOLS):
                continue
            if item.get("name") == "subchat_catalog":
                # This gateway permits only an HTTP catalog observation. The
                # UI picker variant can change the selected profile's default.
                result.append(direct_gateway_catalog()[-1])
            else:
                result.append(item)
        return result

    async def execute(self, grant_id: str, request: Request,
                      granted: frozenset[str]) -> Reply:
        if request.tool not in granted & SUBCHAT_GATEWAY_TOOLS:
            return Reply(operation_id=request.operation_id, state="failed",
                         error="Subchat tool is not granted")
        if request.tool == "subchat_catalog":
            try:
                ReadOnlyHTTPCatalog.model_validate(request.arguments)
            except ValueError:
                return Reply(operation_id=request.operation_id, state="failed",
                             error="Only the HTTP catalog is available through this gateway",
                             data={"dispatched": False})
            request = request.model_copy(update={"arguments": {"source": "http"}})
        # A received request is scheduled independently of the HTTP connection.
        # A repeated ID with different content must never dispatch another input.
        digest = json.dumps(request.arguments, sort_keys=True, separators=(",", ":"))
        key = (grant_id, request.operation_id)
        existing = self.pending.get(key)
        if existing is not None:
            tool, arguments, task = existing
            if (task.done() and not task.cancelled() and task.exception() is None
                    and task.result().data.get("error_code") == "request_conflict"):
                self.pending.pop(key)
                existing = None
        if existing is not None:
            tool, arguments, task = existing
            if (tool, arguments) != (request.tool, digest):
                return Reply(operation_id=request.operation_id, state="failed",
                             error="Operation ID was already used with different input",
                             data={"dispatched": False})
            if task.done() and not task.cancelled() and task.exception() is None:
                reply = task.result()
                sending = getattr(self._core(grant_id), "sends", {}).get(
                    request.operation_id)
                if (request.tool == "subchat_send"
                        and ((reply.state == "failed"
                              and reply.data.get("error_code") in {
                                  "preparation_failed", "request_conflict"}
                              and reply.data.get("dispatched") is False)
                             or (reply.state == "running"
                                 and (sending is None or sending.done())))):
                    # A send ACK is only a checkpoint. Once its core task has
                    # settled, an explicit same-ID call must reach the ledger
                    # guard and see the result or retry unsent preparation.
                    self.pending.pop(key)
                    existing = None
        if existing is None:
            if len(self.pending) >= 128:
                # Completed sends and messages have a durable receipt in the
                # Subchat ledger. The ledger checks their exact arguments when
                # an evicted ID is used again. Never evict live work.
                def durable(entry: tuple[str, str, asyncio.Task[Reply]],
                            operation_id: str) -> bool:
                    tool, _, task = entry
                    if not task.done() or task.cancelled():
                        return False
                    if tool not in {"subchat_send", "subchat_message"}:
                        return True
                    try:
                        reply = task.result()
                    except Exception:
                        return False
                    if reply.state == "failed" and reply.data.get("dispatched") is False:
                        return True
                    return (reply.state == "completed"
                            and reply.data.get("operation_id") == operation_id)

                completed = next((identity for identity, entry in self.pending.items()
                                  if durable(entry, identity[1])), None)
                if completed is not None:
                    self.pending.pop(completed)
                elif request.tool not in {"subchat_send", "subchat_message"}:
                    # Reads can still recover an uncertain write when every
                    # retained ID is unsafe to evict. They need no write cache.
                    observed = await self._core(grant_id).execute(request)
                    if (request.tool == "subchat_recover"
                            and observed.state == "completed"
                            and observed.data.get("state") in {
                                "completed", "cancelled", "interrupted", "preflight_failed"
                            }):
                        target = request.arguments.get("operation_id")
                        if isinstance(target, str):
                            self.pending.pop((grant_id, target), None)
                    return observed
                else:
                    return Reply(operation_id=request.operation_id, state="failed",
                                 error="Recover an uncertain Subchat send before starting "
                                       "another; the gateway has 128 unresolved operations",
                                 data={"dispatched": False})
            async def run() -> Reply:
                reply = await self._core(grant_id).execute(request)
                if request.tool == "subchat_capabilities" and reply.state == "completed":
                    # The direct HTTP gateway does not expose queue_watch.
                    return reply.model_copy(update={"data": {
                        **reply.data, "queue_watch_supported": False,
                    }})
                return reply

            task = asyncio.create_task(run())
            self.pending[key] = (request.tool, digest, task)
        try:
            # Leave room under HTTPMCP's 60-second deadline. Long work remains
            # attached to the service and can be collected by its exact ID.
            return await asyncio.wait_for(asyncio.shield(task), timeout=20)
        except TimeoutError:
            return Reply(operation_id=request.operation_id, state="running",
                         data={"submission_operation_id": request.operation_id,
                               "next_action": "Use subchat_status or subchat_recover "
                                              "with this operation_id"})

    async def close(self) -> None:
        # Closing the service is a lifecycle boundary; HTTP disconnect is not.
        await asyncio.gather(*(core.close() for core in self.cores.values()),
                             return_exceptions=True)
        if self.pending:
            await asyncio.gather(*(entry[2] for entry in self.pending.values()),
                                 return_exceptions=True)
        self.pending.clear()
        self.cores.clear()

    def has_live_work(self) -> bool:
        """Keep owned sends and detached observations alive across HTTP idle gaps."""
        return (any(not entry[2].done() for entry in self.pending.values())
                or any(core.recoveries for core in self.cores.values())
                or any((live := getattr(core, 'live_transport', None)) is not None and live()
                       for core in self.cores.values())
                or any(not task.done() for core in self.cores.values()
                       for task in (*core.calls, *getattr(core, 'sends', {}).values(),
                                    *core.queue_watches.values())))


class LazySubchatGateway:
    """Open a selected account only when discovered; retry failed preparation."""

    def __init__(self, config: SubchatGatewayConfig, *, owner: str,
                 idle_close_seconds: float = _IDLE_CLOSE_SECONDS) -> None:
        self.config = config
        self.owner = owner
        self.account_id = config.account_id
        self._lock = asyncio.Lock()
        self._idle_condition = asyncio.Condition(self._lock)
        self._gateway: SubchatGateway | None = None
        self._resources: AsyncExitStack | None = None
        self._closed = False
        self._retry_after = 0.0
        self._active_calls = 0
        self._idle_task: asyncio.Task[None] | None = None
        self._idle_close_seconds = idle_close_seconds

    async def _acquire(self) -> SubchatGateway | None:
        async with self._lock:
            if self._closed:
                return None
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            if self._gateway is not None:
                self._active_calls += 1
                return self._gateway
            if time.monotonic() < self._retry_after:
                return None
            resources = AsyncExitStack()
            try:
                gateway = await resources.enter_async_context(
                    open_subchat_gateway(self.config, owner=self.owner))
            except asyncio.CancelledError:
                await resources.aclose()
                raise
            except Exception as error:
                await resources.aclose()
                # Provider errors can carry private URLs or account data.
                logger.warning("Subchat gateway unavailable: %s", type(error).__name__)
                self._retry_after = time.monotonic() + 10
                return None
            self._resources = resources
            self._gateway = gateway
            self._active_calls += 1
            return gateway

    async def _release(self) -> None:
        async with self._idle_condition:
            self._active_calls -= 1
            self._idle_condition.notify_all()
            if not self._closed and self._active_calls == 0 and self._resources is not None:
                self._idle_task = asyncio.create_task(self._close_when_idle())

    async def _close_when_idle(self) -> None:
        try:
            await asyncio.sleep(self._idle_close_seconds)
            while True:
                async with self._lock:
                    if self._closed or self._active_calls:
                        return
                    gateway = self._gateway
                    if gateway is not None and not gateway.has_live_work():
                        assert self._resources is not None
                        await self._resources.aclose()
                        self._resources = None
                        self._gateway = None
                        return
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            return
        finally:
            if self._idle_task is asyncio.current_task():
                self._idle_task = None

    async def catalog(self, grant_id: str, granted: frozenset[str]) -> list[JsonValue]:
        if self._closed:
            return []
        return [item for item in direct_gateway_catalog()
                if isinstance(item, dict) and item.get("name") in granted & SUBCHAT_GATEWAY_TOOLS]

    def owner_for_request(self, request: Request, *, stable_owner: str,
                          legacy_grant_id: str,
                          same_principal_grant: Callable[[str], bool] | None = None) -> str:
        """Resolve an exact legacy operation within the authenticated principal."""
        if legacy_grant_id == stable_owner:
            return stable_owner
        target: object = request.operation_id
        if request.tool in {"subchat_status", "subchat_recover", "subchat_wait"}:
            target = request.arguments.get("operation_id")
        elif request.tool == "subchat_message":
            target = request.arguments.get("target_operation_id")
        elif request.tool != "subchat_send":
            return stable_owner
        if not isinstance(target, str):
            return stable_owner
        ledger_path = Path(self.config.ledger)
        database = ledger_path / "operations.sqlite3"
        if ledger_path.is_symlink() or database.is_symlink() or not database.is_file():
            return stable_owner
        try:
            with closing(sqlite3.connect(database.absolute().as_uri() + "?mode=ro",
                                         uri=True)) as connection:
                row = connection.execute(
                    "SELECT owner FROM subchat_submissions WHERE operation_id=?",
                    (target,),
                ).fetchone()
                if row is None or not isinstance(row[0], str):
                    return stable_owner
                legacy_owner = row[0]
                if legacy_owner != legacy_grant_id and (
                    same_principal_grant is None
                    or not same_principal_grant(legacy_owner)
                ):
                    return stable_owner
                store = SubchatSubmissions(connection, initialize=False)
                saved = store.get(target, owner=legacy_owner)
                if legacy_owner != legacy_grant_id:
                    # A queued or submitted child may have no account receipt.
                    # Follow only its exact same-owner ancestry to an account
                    # binding; refuse unbound roots and excessive chains.
                    for _ in range(256):
                        if (saved.provider_account_id is not None
                                or saved.after_operation_id is None):
                            break
                        saved = store.get(saved.after_operation_id, owner=legacy_owner)
                    else:
                        return stable_owner
        except (sqlite3.Error, SubchatOperationNotFound):
            return stable_owner
        if (saved.provider_account_id != self.config.account_id
                and (legacy_owner != legacy_grant_id
                     or saved.provider_account_id is not None)):
            return stable_owner
        return legacy_owner

    async def execute(self, grant_id: str, request: Request,
                      granted: frozenset[str]) -> Reply:
        if request.tool not in granted & SUBCHAT_GATEWAY_TOOLS:
            return Reply(operation_id=request.operation_id, state="failed",
                         error="Subchat tool is not granted")
        if request.tool in {"subchat_status", "subchat_capabilities"}:
            return await self._local_read(grant_id, request)
        gateway = await self._acquire()
        if gateway is None:
            return Reply(operation_id=request.operation_id, state="failed",
                         error="Selected Subchat account is unavailable",
                         data={"dispatched": False})
        try:
            return await gateway.execute(grant_id, request, granted)
        finally:
            await self._release()

    async def _local_read(self, grant_id: str, request: Request) -> Reply:
        """Read configured capabilities or one owned saved operation without Chrome."""
        if self._closed:
            return Reply(operation_id=request.operation_id, state="failed",
                         error="Selected Subchat account is unavailable",
                         data={"dispatched": False})
        try:
            if request.tool == "subchat_capabilities":
                Contract.model_validate(request.arguments)
                from .subchat_browser.backend import browser_capabilities

                return Reply(operation_id=request.operation_id, state="completed",
                             data=capability_report(browser_capabilities(
                                 http_read=True, httpx_generation=True),
                                 queue_watch_supported=False))
            target = OperationId.model_validate(request.arguments)
            queue_watch = None
            gateway = self._gateway
            if gateway is not None:
                core = gateway.cores.get(grant_id)
                if core is not None and target.operation_id in core.queue_watch_states:
                    queue_watch = dict(core.queue_watch_states[target.operation_id])
            saved = await asyncio.to_thread(self._saved_status, grant_id,
                                            request.operation_id, target.operation_id,
                                            queue_watch)
            if saved.data.get("state") == "prepared" and gateway is not None:
                core = gateway.cores.get(grant_id)
                sending = core.sends.get(target.operation_id) if core is not None else None
                if (core is not None and sending is not None and sending.done()
                        and not sending.cancelled() and sending.exception() is not None):
                    # Preserve the live error when it is still available.
                    return await core.execute(request)
            return saved
        except SubchatOperationNotFound:
            return Reply(operation_id=request.operation_id, state="failed",
                         error="No operation with this ID is visible in the selected ledger. "
                               "Check the ID and ledger path, then use subchat_list.",
                         data={"error_code": "unknown_operation", "dispatched": False,
                               "automatic_retry": False})
        except SubchatAccountMismatch:
            return Reply(operation_id=request.operation_id, state="failed",
                         error="The selected Chat account does not match the saved operation. "
                               "Use the original account to inspect this operation.",
                         data={"error_code": "account_mismatch", "automatic_retry": False})
        except ValidationError as error:
            return Reply(operation_id=request.operation_id, state="failed",
                         error="Subchat input has invalid fields. Correct them before sending.",
                         data={"error_code": "invalid_parameter",
                               "invalid_params": [{"path": [str(part) for part in item["loc"]],
                                                   "code": item["type"]}
                                                  for item in error.errors(include_input=False,
                                                                           include_context=False)],
                               "dispatched": False})
        except Exception as error:
            logger.warning("Local Subchat read failed: %s", type(error).__name__)
            return Reply(operation_id=request.operation_id, state="failed",
                         error="Subchat call failed; inspect its saved status before retrying.",
                         data={"error_type": type(error).__name__})

    def _saved_status(self, grant_id: str, request_id: str, operation_id: str,
                      queue_watch: dict[str, JsonValue] | None) -> Reply:
        ledger_path = Path(self.config.ledger)
        database = ledger_path / "operations.sqlite3"
        if ledger_path.is_symlink() or database.is_symlink() or not database.is_file():
            raise SubchatOperationNotFound("Unknown subchat submission")
        with closing(sqlite3.connect(database.absolute().as_uri() + "?mode=ro",
                                     uri=True)) as connection:
            store = SubchatSubmissions(connection, initialize=False)
            result = store.get(operation_id, owner=grant_id)
            if (result.provider_account_id is not None
                    and result.provider_account_id != self.config.account_id):
                raise SubchatAccountMismatch("Saved operation belongs to another account")
            progress = store.http_progress(operation_id, owner=grant_id)
            failure_reason = store.preparation_failure(operation_id, owner=grant_id)
        if failure_reason is not None and result.state in {"prepared", "queued"}:
            return Reply(operation_id=request_id, state="failed",
                         error="Subchat preparation failed; retry the same operation ID "
                               "after correcting the issue.",
                         data={"error_code": "preparation_failed", "dispatched": False,
                               "reason": failure_reason})
        data = result.model_dump(mode="json")
        if progress is not None:
            data["http_progress"] = progress
        if queue_watch is not None:
            data["queue_watch"] = queue_watch
        return Reply(operation_id=request_id, state="completed", data=data)

    async def close(self) -> None:
        async with self._idle_condition:
            self._closed = True
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            while self._active_calls:
                await self._idle_condition.wait()
            if self._resources is not None:
                await self._resources.aclose()
                self._resources = None
                self._gateway = None


@asynccontextmanager
async def lazy_subchat_gateway(config: SubchatGatewayConfig, *, owner: str
                               ) -> AsyncIterator[LazySubchatGateway]:
    gateway = LazySubchatGateway(config, owner=owner)
    try:
        yield gateway
    finally:
        await gateway.close()


async def _background_account_session(context: BrowserContext, client: httpx.AsyncClient,
                                      account_id: str) -> None:
    """Keep account checks from opening a normal Chrome tab."""
    from .subchat_browser.background import new_background_page
    from .subchat_chrome_login import chrome_http_session

    await chrome_http_session(context, client, expected_account_id=account_id,
                              page_factory=lambda: new_background_page(context))


@asynccontextmanager
async def open_subchat_gateway(config: SubchatGatewayConfig, *, owner: str
                               ) -> AsyncIterator[SubchatGateway]:
    """Use one selected local profile and ledger; never acquire login credentials."""
    profile = Path(config.profile)
    ledger_path = Path(config.ledger)
    if (sys.platform != "darwin" or not profile.is_dir() or profile.is_symlink()
            or ledger_path.is_symlink()
            or (ledger_path.exists() and not ledger_path.is_dir())):
        raise ValueError("Selected Subchat profile or ledger is unavailable")
    import httpx
    from playwright.async_api import APIRequestContext, async_playwright

    from .state import Ledger
    from .subchat import Subchats
    from .subchat_browser.backend import BrowserSubchatBackend
    from .subchat_browser.background import background_chrome_context
    from .subchat_chrome_profile import temporary_chrome_profile
    from .subchat_mcp import session
    from .subchat_state import SubchatSubmissions

    async with AsyncExitStack() as resources:
        ledger = Ledger(ledger_path)
        resources.callback(ledger.close)
        playwright = await resources.enter_async_context(async_playwright())
        snapshot = await resources.enter_async_context(temporary_chrome_profile(profile))
        context = await resources.enter_async_context(background_chrome_context(
            playwright, snapshot, [f"--profile-directory={profile.name}"]))
        # Pin the selected account before exposing any tool. A copied profile with
        # missing or different credentials fails startup rather than drifting.
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            await _background_account_session(context, client, config.account_id)
        request_context = await playwright.request.new_context()
        resources.push_async_callback(request_context.dispose)

        async def request_factory() -> APIRequestContext:
            return request_context

        store = SubchatSubmissions(ledger.connection)

        async def checked_account() -> None:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0),
            ) as client:
                await _background_account_session(context, client, config.account_id)

        def core_factory(grant_id: str) -> SubchatSession:
            def record_request(operation_id: str, message_id: str, account_id: str) -> None:
                store.observe_request(operation_id, message_id, owner=grant_id,
                                      provider_account_id=account_id)

            def record_conversation(operation_id: str, message_id: str,
                                    conversation_id: str, account_id: str) -> None:
                store.observe_conversation(operation_id, message_id, conversation_id,
                                           owner=grant_id, provider_account_id=account_id)

            def record_rejection(operation_id: str, message_id: str,
                                 status: int, account_id: str) -> None:
                store.observe_rejection(operation_id, message_id, status,
                                        owner=grant_id, provider_account_id=account_id)

            def record_preflight_failure(operation_id: str) -> None:
                if not store.fail_http_before_dispatch(operation_id, owner=grant_id):
                    raise ValueError("Generation preflight state changed")

            backend = BrowserSubchatBackend(
                context, http_read=True, http_request_factory=request_factory,
                httpx_generation=True, background_pages=True, store=store,
                owner=grant_id, expected_account_id=config.account_id,
                record_request=record_request,
                record_conversation=record_conversation,
                record_rejection=record_rejection,
                record_preflight_failure=record_preflight_failure)

            async def catalog(model: str | None) -> dict[str, object]:
                await checked_account()
                return await backend.catalog(model)

            async def http_catalog() -> dict[str, object]:
                await checked_account()
                return await backend.http_catalog()

            return session(Subchats(store, backend), observe_catalog=catalog,
                           observe_http_catalog=http_catalog,
                           owner=grant_id, serialize_recovery=True)

        gateway = SubchatGateway(core_factory, owner=owner, account_id=config.account_id)
        try:
            yield gateway
        finally:
            await gateway.close()
