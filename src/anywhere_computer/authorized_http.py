"""Bind verified HTTP grants to one owner's device and its current tool permissions."""

import asyncio
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from pydantic import JsonValue, ValidationError

from .authorization import AuthorizationStore, GrantIdentity
from .connection import ensure_agent, exchange_remote
from .delegated_files import read as delegated_read
from .delegated_files import write as delegated_write
from .delegated_routes import DelegatedRouteStore
from .delegated_tasks import DelegatedTaskStore
from .device_router import NESTED_REQUEST_ID_ERROR, ROUTER_TOOLS, DeviceBackend, DeviceRouter
from .devices import DeviceData
from .engine import Engine
from .engine_selection import engine_directory
from .http_client import ChildBearerTokens, HTTPBackend
from .mcp_server import INSTRUCTIONS as MCP_INSTRUCTIONS
from .mcp_server import OPERATION_META, REQUEST_ID_SCHEMA, MCPSession, rpc_error
from .models import OperationId, ReadFile, Reply, Request, RuntimeSettings, WriteFile
from .remote_bridge import RemoteAgent
from .ssh_client import SSHBackend
from .state import Ledger
from .subchat_device_adapters import RoutedSaveTarget, SubchatSaveSource
from .subchat_device_save import SUBCHAT_SAVE_TOOLS, DeviceSave, SaveJournal, SaveRunner
from .subchat_gateway import (
    SUBCHAT_AUTH_SCOPES,
    SUBCHAT_GATEWAY_TOOLS,
    LazySubchatGateway,
    SubchatGateway,
    subchat_ledger_owner,
)


class SubchatHTTPSession(MCPSession):
    """Require a client-known ID before dispatching an ordinary Chat input."""

    async def handle(self, packet: JsonValue) -> dict[str, JsonValue] | None:
        if (isinstance(packet, dict) and "id" in packet
                and packet.get("jsonrpc") == "2.0"
                and packet.get("method") == "tools/call"):
            params = packet.get("params")
            if isinstance(params, dict) and params.get("name") in {
                "subchat_send", "subchat_message", "subchat_save_file",
            }:
                arguments = params.get("arguments")
                metadata = params.get("_meta")
                supplied = (isinstance(arguments, dict) and "request_id" in arguments)
                supplied = supplied or (isinstance(metadata, dict)
                                        and OPERATION_META in metadata)
                if not supplied:
                    return rpc_error(packet.get("id"), -32602,
                                     "Choose request_id before sending a Subchat input")
        return await super().handle(packet)


class AuthorizedDeviceMCP:
    def __init__(
        self,
        store: AuthorizationStore,
        engine: Engine | None = None,
        *,
        agent_directory: Path | None = None,
        owner: str,
        device: str,
        client: str | None = None,
        allowed_tools: frozenset[str] | None = None,
        device_directory: Path | None = None,
        subchat_gateway: SubchatGateway | LazySubchatGateway | None = None,
        delegated_tasks: DelegatedTaskStore | None = None,
    ) -> None:
        if (engine is None) == (agent_directory is None):
            raise ValueError("Select exactly one embedded engine or shared agent directory")
        self.store = store
        self.engine = engine
        self.agent_directory = agent_directory
        self.owner = owner
        self.device = device
        self.client, self.allowed_tools = client, allowed_tools
        self.device_directory = device_directory
        self.subchat_gateway = subchat_gateway
        self.delegated_tasks = delegated_tasks
        self._save_tasks: dict[tuple[str, str], tuple[DeviceSave, asyncio.Task[Reply]]] = {}
        self._delegated_file_tasks: set[asyncio.Task[Reply]] = set()

    def _matches(self, grant: GrantIdentity | None) -> bool:
        return (
            grant is not None
            and grant.owner == self.owner
            and grant.device == self.device
            and (self.client is None or grant.client == self.client)
            and (self.allowed_tools is None or grant.tools <= self.allowed_tools)
        )

    async def authenticate(self, token: str) -> str | None:
        grant = self.store.verify(token, resource=self.store.resource)
        if grant is not None and self._matches(grant):
            return grant.grant_id
        if self.delegated_tasks is not None:
            child_id = self.delegated_tasks.verify_token(token)
            if child_id is not None:
                child = self.delegated_tasks.current(child_id)
                parent = (self.store.current_grant(child.parent_grant_id)
                          if child is not None else None)
                if self._matches(parent):
                    return "child:" + child_id
        return None

    def session(self, grant_id: str) -> MCPSession:
        if grant_id.startswith("child:"):
            return self._child_session(grant_id.removeprefix("child:"))
        def current() -> GrantIdentity:
            grant = self.store.current_grant(grant_id)
            if grant is None or not self._matches(grant):
                raise ValueError("Device authorization is no longer available")
            return grant

        async def local_catalog() -> list[JsonValue]:
            grant = current()
            if self.engine is not None:
                return self.engine.catalog(grant.tools - ROUTER_TOOLS - SUBCHAT_AUTH_SCOPES)
            if self.agent_directory is None:
                raise RuntimeError("No engine was configured")
            # Retained HTTP sessions must recover before MCP's pre-dispatch catalog
            # check. Reuse the selected agent without replacing live work. The grant
            # is checked again by local_execute after waiting for startup.
            await asyncio.to_thread(ensure_agent, self.agent_directory, replace_idle=False)
            reply = await local_execute(Request(operation_id=uuid.uuid4().hex, tool="__catalog"))
            tools = reply.data.get("tools")
            if reply.state != "completed" or not isinstance(tools, list):
                raise ConnectionError("Shared engine catalog is unavailable")
            return [item for item in tools if isinstance(item, dict)
                    and item.get("name") not in SUBCHAT_GATEWAY_TOOLS]

        async def local_execute(request: Request) -> Reply:
            grant = current()
            if self.agent_directory is not None:
                return await exchange_remote(
                    self.agent_directory, grant.grant_id,
                    grant.tools - ROUTER_TOOLS - SUBCHAT_AUTH_SCOPES
                    - SUBCHAT_SAVE_TOOLS, request,
                    authorization_database=(self.store.database
                                            if request.tool == 'mcp_session_open' else None),
                )
            if self.engine is None:
                raise RuntimeError("No engine was configured")
            if request.tool == 'mcp_session_open':
                self.engine.bind_http_watch_grant(grant.grant_id, self.store.database)
            # Reuse the peer namespace and lookup checks; grants are reloaded per dispatch.
            bridge = RemoteAgent(
                self.engine, {grant.grant_id: grant.tools - ROUTER_TOOLS
                                                - SUBCHAT_AUTH_SCOPES
                                                - SUBCHAT_SAVE_TOOLS}, transport="http",
            )
            return Reply.model_validate_json(
                await bridge.dispatch(grant.grant_id, request.model_dump_json().encode())
            )

        async def catalog() -> list[JsonValue]:
            grant = current()
            granted = grant.tools
            subchat = (await self.subchat_gateway.catalog(
                subchat_ledger_owner(grant, self.subchat_gateway.account_id), granted)
                       if self.subchat_gateway is not None else [])
            if self.subchat_gateway is not None and "subchat_save_file" in granted:
                schema = DeviceSave.model_json_schema()
                schema["properties"]["request_id"] = dict(REQUEST_ID_SCHEMA)
                schema.setdefault("required", []).append("request_id")
                subchat.append({
                    "name": "subchat_save_file",
                    "description": "Save one verified final-answer sandbox file to an unused "
                                   "absolute path on an explicitly selected device. "
                                   "Returns status and metadata without file bytes. "
                                   "Reuse the same request_id to observe pending work.",
                    "inputSchema": schema,
                    "annotations": {"readOnlyHint": False, "destructiveHint": True,
                                    "openWorldHint": False},
                })
            if self.device_directory is None or not granted & ROUTER_TOOLS:
                return [*await local_catalog(), *subchat]
            router = DeviceRouter(self.device_directory, local_catalog, local_execute)
            try:
                return [item for item in await router.catalog()
                        if isinstance(item, dict) and item.get("name") in granted] + subchat
            finally:
                router.close()

        async def execute(request: Request) -> Reply:
            grant = current()
            if request.tool in SUBCHAT_SAVE_TOOLS:
                if (request.tool not in grant.tools or not isinstance(
                        self.subchat_gateway, LazySubchatGateway)
                        or self.device_directory is None):
                    return Reply(operation_id=request.operation_id, state="failed",
                                 error="Direct file save is unavailable")
                try:
                    args = DeviceSave.model_validate(request.arguments)
                except ValueError:
                    return Reply(operation_id=request.operation_id, state="failed",
                                 error="Invalid direct file save arguments",
                                 data={"dispatched": False})
                principal = SaveJournal._principal(grant)
                key = (principal, request.operation_id)
                pending = self._save_tasks.get(key)
                if pending is not None and pending[0] != args:
                    return Reply(operation_id=request.operation_id, state="failed",
                                 error="Save ID was already used with different input",
                                 data={"dispatched": False})
                if pending is None or pending[1].done():
                    async def perform() -> Reply:
                        assert isinstance(self.subchat_gateway, LazySubchatGateway)
                        assert self.device_directory is not None
                        async def save_local_execute(inner: Request) -> Reply:
                            active = current()
                            allowed = (active.tools - ROUTER_TOOLS - SUBCHAT_AUTH_SCOPES
                                       - SUBCHAT_SAVE_TOOLS)
                            identity = "subchat-save:" + principal
                            if self.agent_directory is not None:
                                return await exchange_remote(self.agent_directory, identity,
                                                             allowed, inner)
                            assert self.engine is not None
                            bridge = RemoteAgent(self.engine, {identity: allowed},
                                                 transport="http")
                            return Reply.model_validate_json(await bridge.dispatch(
                                identity, inner.model_dump_json().encode()))

                        target = RoutedSaveTarget(self.device_directory, local_catalog,
                                                  save_local_execute, current, args.device_id)
                        source = SubchatSaveSource(
                            self.subchat_gateway, current,
                            lambda active, candidate: self.store.same_principal_grant(
                                active, candidate),
                        )
                        journal = SaveJournal(
                            self.store.database.parent.parent / "subchat-device-saves.sqlite3")
                        try:
                            async def authorize_save() -> tuple[GrantIdentity, str]:
                                active = current()
                                return active, await target.authorize()

                            runner = SaveRunner(
                                journal, authorize=authorize_save, download=source.download,
                                target=target, account_id=self.subchat_gateway.account_id,
                                spool_directory=(self.store.database.parent.parent
                                                 / "subchat-device-spool"),
                            )
                            outcome = await runner.advance(request.operation_id, args)
                            state = outcome.get("state")
                            reply_state = ("completed" if state == "completed" else
                                           "unknown" if state == "unknown" else "running")
                            return Reply.model_validate({"operation_id": request.operation_id,
                                                         "state": reply_state, "data": outcome})
                        except PermissionError:
                            return Reply(operation_id=request.operation_id, state="failed",
                                         error="Direct save authorization is unavailable")
                        except (OSError, ValueError, RuntimeError, ConnectionError):
                            return Reply(operation_id=request.operation_id, state="unknown",
                                         error="Direct save outcome is unconfirmed; inspect "
                                               "the same request_id and target upload status.")
                        finally:
                            journal.close()

                    task = asyncio.create_task(perform())
                    self._save_tasks[key] = (args, task)
                else:
                    task = pending[1]
                try:
                    return await asyncio.wait_for(asyncio.shield(task), 2.0)
                except TimeoutError:
                    return Reply(operation_id=request.operation_id, state="running",
                                 data={"state": "running", "result_pending": True,
                                       "next_action": "Reuse the same request_id to inspect "
                                                      "the save; do not start another."})
            if request.tool in SUBCHAT_GATEWAY_TOOLS:
                if self.subchat_gateway is None or grant.owner != self.subchat_gateway.owner:
                    return Reply(operation_id=request.operation_id, state="failed",
                                 error="Subchat gateway is unavailable")
                subchat_owner = subchat_ledger_owner(grant, self.subchat_gateway.account_id)
                if (request.tool in grant.tools
                        and isinstance(self.subchat_gateway, LazySubchatGateway)):
                    subchat_owner = await asyncio.to_thread(
                        self.subchat_gateway.owner_for_request, request,
                        stable_owner=subchat_owner, legacy_grant_id=grant.grant_id,
                        same_principal_grant=lambda candidate: self.store.same_principal_grant(
                            grant, candidate),
                    )
                return await self.subchat_gateway.execute(
                    subchat_owner, request, grant.tools,
                    **({'authorization_grant_id': grant.grant_id}
                       if request.tool == 'subchat_queue_auto' else {}))
            if request.tool not in ROUTER_TOOLS:
                return await local_execute(request)
            if self.device_directory is None or request.tool not in grant.tools:
                return Reply(operation_id=request.operation_id, state="failed",
                             error="Device routing is not granted to this connection")
            # Saved target credentials can be shared by multiple HTTP grants. Keep their
            # routed operation IDs separate even when the target uses one credential.
            namespace = "device-route:" + grant.grant_id
            arguments = dict(request.arguments)
            nested = arguments.get("arguments")
            if (request.tool == "devices_call" and isinstance(nested, dict)
                    and "request_id" in nested):
                return Reply(operation_id=request.operation_id, state="failed",
                             data={"dispatched": False}, error=NESTED_REQUEST_ID_ERROR)
            lookup = None
            try:
                if request.tool == "devices_call" and arguments.get("tool") == "operations_get":
                    lookup = OperationId.model_validate(arguments.get("arguments")).operation_id
                    arguments["arguments"] = {
                        "operation_id": RemoteAgent.internal_id(namespace, lookup),
                    }
                forwarded = Request(
                    operation_id=RemoteAgent.internal_id(namespace, request.operation_id),
                    tool=request.tool, arguments=arguments,
                )
            except ValueError:
                return Reply(operation_id=request.operation_id, state="failed",
                             error="Invalid routed operation lookup")
            router = DeviceRouter(self.device_directory, local_catalog, local_execute)
            try:
                result = await router.execute(forwarded)
            finally:
                router.close()
            data = dict(result.data)
            recovered = data.get("result")
            if lookup is not None and isinstance(recovered, dict) and "operation_id" in recovered:
                data["result"] = {**recovered, "operation_id": lookup}
            return result.model_copy(update={"operation_id": request.operation_id, "data": data})

        instructions = MCP_INSTRUCTIONS
        if self.subchat_gateway is not None:
            instructions += (
                " For Subchat, choose the request_id before subchat_send or "
                "subchat_message. Save that exact operation ID. A running or submitted "
                "reply is not a final answer. Use subchat_status or subchat_observe "
                "with the saved ID; recover/wait can dispatch a queued follow-up. "
                "Never resend an uncertain input. "
                "The selected Chrome profile supplies browser preparation; generation "
                "uses browser-prepared HTTPX, not independent provider authorization."
            )
        session_type = SubchatHTTPSession if self.subchat_gateway is not None else MCPSession
        return session_type(catalog, execute, instructions=instructions)

    async def close(self) -> None:
        if self._delegated_file_tasks:
            await asyncio.gather(*self._delegated_file_tasks, return_exceptions=True)
            self._delegated_file_tasks.clear()
        if self._save_tasks:
            await asyncio.gather(*(entry[1] for entry in self._save_tasks.values()),
                                 return_exceptions=True)
            self._save_tasks.clear()

    def _child_session(self, child_id: str) -> MCPSession:
        delegation = self.delegated_tasks
        if delegation is None:
            raise ValueError("Delegated task authority is unavailable")
        active_delegation: DelegatedTaskStore = delegation
        child = delegation.current(child_id)
        if child is None or not self._matches(self.store.current_grant(child.parent_grant_id)):
            raise ValueError("Delegated task authorization is unavailable")
        parent_session = self.session(child.parent_grant_id)
        namespace = "delegated-child:" + child_id

        def current_file_limits() -> RuntimeSettings:
            if self.engine is not None:
                return self.engine.settings()
            if self.agent_directory is None:
                raise ValueError("Delegated file settings are unavailable")
            try:
                database = engine_directory(self.agent_directory) / "operations.sqlite3"
            except RuntimeError as error:
                raise ValueError("Delegated file settings are unavailable") from error
            if database.is_symlink() or not database.is_file():
                raise ValueError("Delegated file settings are unavailable")
            try:
                with closing(sqlite3.connect(database.absolute().as_uri() + "?mode=ro",
                                             uri=True, timeout=5)) as connection:
                    row = connection.execute(
                        "SELECT value FROM runtime_settings WHERE id=1").fetchone()
            except sqlite3.Error:
                raise ValueError("Delegated file settings are unavailable") from None
            return RuntimeSettings.model_validate_json(row[0]) if row else RuntimeSettings()

        async def catalog() -> list[JsonValue]:
            active = delegation.current(child_id)
            if (active is None or not self._matches(
                    self.store.current_grant(active.parent_grant_id))):
                raise ValueError("Delegated task authorization is unavailable")
            tools = await parent_session.catalog()
            visible = (active.tools if active.device_id == "local"
                       else frozenset({"devices_call"}))
            selected: list[JsonValue] = [
                tool for tool in tools if isinstance(tool, dict)
                and tool.get("name") in visible]
            selected.append({
                'name': 'delegated_identity',
                'description': 'Read the authenticated child grant identity for route binding.',
                'inputSchema': {'type': 'object', 'properties': {},
                                'additionalProperties': False},
                'outputSchema': Reply.model_json_schema(),
                'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                                'idempotentHint': True, 'openWorldHint': False},
            })
            return selected

        async def execute(request: Request) -> Reply:
            try:
                delegation.bind_request(child_id, request)
            except ValueError:
                return Reply(operation_id=request.operation_id, state="failed",
                             data={"dispatched": False,
                                   "reason": "operation_id_conflict"},
                             error="Delegated operation ID was used for another request")
            if request.tool == 'delegated_identity':
                active = delegation.current(child_id)
                if (request.arguments or active is None or not self._matches(
                        self.store.current_grant(active.parent_grant_id))):
                    return Reply(operation_id=request.operation_id, state='failed',
                                 data={'dispatched': False},
                                 error='Delegated identity is unavailable')
                return Reply(operation_id=request.operation_id, state='completed',
                             data={'child_id': child_id, 'device_id': active.device_id})
            tool_request = request
            device_id = "local"
            arguments = dict(request.arguments)
            if request.tool == "devices_call":
                device_id = str(arguments.get("device_id", ""))
                name = arguments.get("tool")
                nested = arguments.get("arguments")
                if not isinstance(name, str) or not isinstance(nested, dict):
                    return Reply(operation_id=request.operation_id, state="failed",
                                 data={"dispatched": False},
                                 error="Invalid delegated device call")
                tool_request = Request(operation_id=request.operation_id, tool=name,
                                       arguments=nested)
            denied = delegation.check(child_id, device_id, tool_request,
                                      ledger=delegation.ledger,
                                      recorded_request=request)
            if denied is not None:
                return denied
            if device_id == "local" and tool_request.tool in {"files_read", "files_write"}:
                internal_id = RemoteAgent.internal_id(namespace, request.operation_id)
                recorded = request.model_copy(update={"operation_id": internal_id})
                try:
                    previous = delegation.ledger.claim(recorded)
                except ValueError:
                    return Reply(operation_id=request.operation_id, state="failed",
                                 data={"dispatched": False, "reason": "operation_id_conflict"},
                                 error="Delegated operation ID was used for another request")
                if previous is not None:
                    return previous.model_copy(update={"operation_id": request.operation_id})
                async def finish_file_operation() -> Reply:
                    # HTTP request cancellation must not abandon a running file worker.
                    # Its result stays retrievable by the original operation ID.
                    try:
                        settings = (current_file_limits() if self.engine is not None
                                    else await asyncio.to_thread(current_file_limits))

                        def perform_file() -> dict[str, JsonValue]:
                            with delegation.local_file_guard(child_id, tool_request) as active:
                                if tool_request.tool == "files_read":
                                    file_args = ReadFile.model_validate(tool_request.arguments)
                                    file_args = file_args.model_copy(update={
                                        "limit": min(file_args.limit,
                                                     settings.file_read_line_limit),
                                    })
                                    return delegated_read(file_args, active.read_roots)
                                file_args_write = WriteFile.model_validate(tool_request.arguments)
                                if (len(file_args_write.text.splitlines())
                                        > settings.file_write_line_limit):
                                    raise ValueError("Write exceeds configured line limit")
                                return delegated_write(file_args_write, active.write_roots,
                                                       delegation.directory / "file-backups")

                        data = await asyncio.to_thread(perform_file)
                        reply = Reply(operation_id=internal_id, state="completed", data=data)
                    except sqlite3.Error:
                        reply = Reply(operation_id=internal_id, state="failed",
                                      error="Delegated authorization store is unavailable",
                                      data={"dispatched": False})
                    except (OSError, ValueError) as error:
                        late_denial = (delegation.check(child_id, "local", tool_request,
                                                        ledger=delegation.ledger)
                                       if delegation.current(child_id) is None else None)
                        if late_denial is not None:
                            reply = late_denial.model_copy(update={"operation_id": internal_id})
                        else:
                            reply = Reply(operation_id=internal_id, state="failed",
                                          error=str(error), data={"dispatched": False})
                    delegation.ledger.finish(reply)
                    return reply.model_copy(update={"operation_id": request.operation_id})

                file_task = asyncio.create_task(finish_file_operation())
                self._delegated_file_tasks.add(file_task)
                file_task.add_done_callback(self._delegated_file_tasks.discard)
                return await asyncio.shield(file_task)
            lookup: str | None = None
            if tool_request.tool == "operations_get":
                try:
                    lookup = OperationId.model_validate(tool_request.arguments).operation_id
                except ValueError:
                    return Reply(operation_id=request.operation_id, state="failed",
                                 data={"dispatched": False},
                                 error="Invalid delegated operation lookup")
                if device_id == "local":
                    def saved_local_result(saved: Reply, internal_id: str) -> Reply:
                        data = saved.model_copy(update={
                            "operation_id": lookup}).model_dump(mode="json")
                        data["recorded_tool"] = delegation.ledger.tool_for(internal_id)
                        data["request_digest"] = delegation.ledger.digest_for(internal_id)
                        return Reply(operation_id=request.operation_id, state="completed",
                                     data=data)

                    denial_id = RemoteAgent.internal_id(
                        "delegated-denial:" + child_id, lookup)
                    original_id = RemoteAgent.internal_id(namespace, lookup)
                    try:
                        old_denial = delegation.ledger.get(denial_id)
                    except ValueError:
                        old_denial = None
                    try:
                        saved = delegation.ledger.get(original_id)
                    except ValueError:
                        saved = None
                    if old_denial is not None and saved is not None:
                        return Reply(operation_id=request.operation_id, state="failed",
                                     data={"dispatched": False,
                                           "reason": "operation_id_ambiguous"},
                                     error="Legacy delegated operation has conflicting "
                                           "receipts; inspect the local audit")
                    if old_denial is not None:
                        return saved_local_result(old_denial, denial_id)
                    if saved is not None:
                        return saved_local_result(saved, original_id)
                    arguments["operation_id"] = RemoteAgent.internal_id(namespace, lookup)
                else:
                    binding = delegation.remote_operation(child_id, lookup)
                    if binding is None:
                        try:
                            source_saved = delegation.ledger.get(
                                RemoteAgent.internal_id(namespace, lookup))
                        except ValueError:
                            source_saved = None
                        if source_saved is None or source_saved.state in {"running", "unknown"}:
                            return Reply(
                                operation_id=request.operation_id, state="unknown",
                                data={"reason": "target_binding_unavailable",
                                      "automatic_retry": False},
                                error="Original target binding is unavailable; do not resend")
                    if binding is not None and binding[0] != device_id:
                        return Reply(operation_id=request.operation_id, state="failed",
                                     data={"dispatched": False,
                                           "reason": "device_out_of_scope"},
                                     error="Original delegated operation used another device")
                    try:
                        saved = delegation.ledger.get(RemoteAgent.internal_id(
                            namespace, lookup))
                    except ValueError:
                        pass
                    else:
                        if saved.state in {"completed", "failed"}:
                            return Reply(operation_id=request.operation_id,
                                         state="completed", data=saved.model_copy(update={
                                             "operation_id": lookup}).model_dump(mode="json"))
                    nested_arguments = dict(tool_request.arguments)
                    nested_arguments["operation_id"] = RemoteAgent.internal_id(
                        namespace, lookup)
                    arguments["arguments"] = nested_arguments
            forwarded = request.model_copy(update={
                "operation_id": RemoteAgent.internal_id(namespace, request.operation_id),
                "arguments": arguments,
            })
            if device_id == "local":
                result = await parent_session.execute(forwarded)
            else:
                try:
                    # Keep the source receipt keyed to the caller's exact input.
                    # operations_get rewrites its nested lookup only for routing.
                    previous = delegation.ledger.claim(request.model_copy(update={
                        "operation_id": forwarded.operation_id}))
                except ValueError:
                    return Reply(operation_id=request.operation_id, state="failed",
                                 data={"dispatched": False, "reason": "operation_id_conflict"},
                                 error="Delegated operation ID was used for another request")
                if previous is not None:
                    if previous.state in {"running", "unknown"}:
                        return Reply(operation_id=request.operation_id, state="unknown",
                                     data={"next_action": (
                                         "Use operations_get with this operation ID")},
                                     error="Delegated target outcome is not yet known")
                    return previous.model_copy(update={"operation_id": request.operation_id})
                if self.device_directory is None:
                    result = Reply(operation_id=forwarded.operation_id, state="failed",
                                   data={"dispatched": False},
                                   error="Delegated target route is unavailable")
                    delegation.ledger.finish(result)
                    return result.model_copy(update={"operation_id": request.operation_id})
                routes = DelegatedRouteStore(self.device_directory / 'delegated-routes')
                try:
                    def child_backend(device_data: DeviceData) -> DeviceBackend:
                        if (device_data['device_id'] != device_id
                                or device_data['transport'] not in {'http', 'ssh'}):
                            raise ValueError('Delegated target identity is unavailable')
                        if device_data['transport'] == 'ssh':
                            host = device_data['ssh_host']
                            if not isinstance(host, str):
                                raise ValueError('Delegated SSH target is invalid')
                            route_identity = 'ssh:' + host
                            bearer = routes.bearer(child_id, device_id, route_identity)
                            target: DeviceBackend = SSHBackend(host, child_bearer=bearer)
                        else:
                            resource = device_data['resource']
                            if not isinstance(resource, str):
                                raise ValueError('Delegated HTTP target is invalid')
                            route_identity = resource
                            bearer = routes.bearer(child_id, device_id, route_identity)
                            target = HTTPBackend(ChildBearerTokens(resource, bearer))

                        class CheckedChildBackend:
                            async def catalog(self) -> list[JsonValue]:
                                catalog = await target.catalog()
                                if (tool_request.tool == 'files_write'
                                        and not any(isinstance(item, dict)
                                                    and item.get('name') == 'operations_get'
                                                    for item in catalog)):
                                    raise ValueError('Target child cannot recover remote writes')
                                return catalog

                            async def execute(self, forwarded_request: Request) -> Reply:
                                expected_target_id = routes.target_child_id(child_id, device_id)
                                if expected_target_id is None:
                                    return Reply(
                                        operation_id=forwarded_request.operation_id,
                                        state='failed',
                                        data={'dispatched': False,
                                              'reason': 'target_identity_unverified'},
                                        error='Target child identity was not recorded for '
                                              'this route')
                                if lookup is not None:
                                    original_binding = active_delegation.remote_operation(
                                        child_id, lookup)
                                    if (original_binding is not None
                                            and original_binding[2] != expected_target_id):
                                        return Reply(
                                            operation_id=forwarded_request.operation_id,
                                            state='failed',
                                            data={'dispatched': False,
                                                  'reason': 'target_identity_changed'},
                                            error='Original delegated target child changed')
                                # Catalog and reconnect may await. Recheck source child,
                                # parent and route immediately before target dispatch.
                                denied = active_delegation.check(
                                    child_id, device_id, tool_request,
                                    ledger=active_delegation.ledger,
                                    recorded_request=request)
                                if denied is not None:
                                    return denied.model_copy(update={
                                        'operation_id': forwarded_request.operation_id})
                                try:
                                    routes.bearer(child_id, device_id, route_identity)
                                except ValueError:
                                    return Reply(
                                        operation_id=forwarded_request.operation_id,
                                        state='failed',
                                        data={'dispatched': False,
                                              'reason': 'route_unavailable'},
                                        error='Delegated target route was unavailable '
                                              'before dispatch')
                                observed = await target.execute(Request(
                                    operation_id=uuid.uuid4().hex,
                                    tool='delegated_identity', arguments={}))
                                if (observed.state != 'completed'
                                        or observed.data.get('child_id') != expected_target_id
                                        or observed.data.get('device_id') != 'local'):
                                    return Reply(
                                        operation_id=forwarded_request.operation_id,
                                        state='failed',
                                        data={'dispatched': False,
                                              'reason': 'target_identity_unverified'},
                                        error='Authenticated target child identity did not '
                                              'match the saved route')
                                denied = active_delegation.check(
                                    child_id, device_id, tool_request,
                                    ledger=active_delegation.ledger,
                                    recorded_request=request)
                                if denied is not None:
                                    return denied.model_copy(update={
                                        'operation_id': forwarded_request.operation_id})
                                try:
                                    routes.bearer(child_id, device_id, route_identity)
                                except ValueError:
                                    return Reply(
                                        operation_id=forwarded_request.operation_id,
                                        state='failed',
                                        data={'dispatched': False,
                                              'reason': 'route_unavailable'},
                                        error='Delegated target route was unavailable '
                                              'before dispatch')
                                if lookup is None:
                                    active_delegation.bind_remote_operation(
                                        child_id, request.operation_id, device_id,
                                        tool_request.tool, expected_target_id,
                                        Ledger.request_digest(Request(
                                            operation_id=forwarded_request.operation_id,
                                            tool=tool_request.tool,
                                            arguments=tool_request.arguments)))
                                return await target.execute(forwarded_request)

                            async def close(self) -> None:
                                await target.close()

                        return CheckedChildBackend()

                    async def unavailable_catalog() -> list[JsonValue]:
                        return []

                    async def unavailable_execute(_request: Request) -> Reply:
                        raise ValueError('Delegated remote call cannot run locally')

                    router = DeviceRouter(self.device_directory, unavailable_catalog,
                                          unavailable_execute, backend_factory=child_backend)
                    try:
                        result = await router.execute(forwarded)
                    finally:
                        router.close()
                finally:
                    routes.close()
            data = dict(result.data)
            if lookup is not None and device_id != "local" and result.state == "completed":
                target_saved = data.get("result")
                binding = delegation.remote_operation(child_id, lookup)
                if (not isinstance(target_saved, dict) or binding is None
                        or target_saved.get("operation_id") != RemoteAgent.internal_id(
                            namespace, lookup)
                        or target_saved.get("recorded_tool") != binding[1]
                        or target_saved.get("request_digest") != binding[3]):
                    mismatch = Reply(
                        operation_id=forwarded.operation_id, state="failed",
                        data={"reason": "target_result_mismatch",
                              "automatic_retry": False},
                        error="Target result did not match the original delegated request")
                    delegation.ledger.finish(mismatch)
                    return mismatch.model_copy(update={"operation_id": request.operation_id})
            if lookup is not None:
                recovered = (data.get("result") if device_id != "local" else data)
                if isinstance(recovered, dict) and recovered.get("operation_id") == (
                    RemoteAgent.internal_id(namespace, lookup)
                ):
                    binding = (delegation.remote_operation(child_id, lookup)
                               if device_id != "local" else None)
                    if device_id != "local" and result.state == "completed":
                        try:
                            target_reply = Reply.model_validate({
                                key: recovered[key]
                                for key in ("operation_id", "state", "data", "error")
                            })
                            original_id = RemoteAgent.internal_id(namespace, lookup)
                            original = delegation.ledger.get(original_id)
                        except (ValidationError, ValueError, KeyError):
                            mismatch = Reply(
                                operation_id=forwarded.operation_id, state="failed",
                                data={"reason": "target_result_mismatch",
                                      "automatic_retry": False},
                                error="Target result did not match the original delegated "
                                      "request")
                            delegation.ledger.finish(mismatch)
                            return mismatch.model_copy(update={
                                "operation_id": request.operation_id})
                        else:
                            if (target_reply.state in {"completed", "failed"}
                                    and original.state in {"running", "unknown"}):
                                delegation.ledger.finish(Reply(
                                    operation_id=original_id, state=target_reply.state,
                                    data={"device_id": device_id,
                                          "result": target_reply.data},
                                    error=target_reply.error,
                                ))
                    recovered = {**recovered, "operation_id": lookup}
                    if device_id != "local":
                        data["result"] = recovered
                    else:
                        data = recovered
            if device_id != "local":
                delegation.ledger.finish(result.model_copy(update={
                    "operation_id": forwarded.operation_id, "data": data}))
                if result.state == "failed":
                    remote_data = result.data.get('result')
                    reason = ('route_unavailable' if isinstance(remote_data, dict)
                              and remote_data.get('reason') == 'route_unavailable'
                              else 'target_failed')
                    delegation.record_target_failure(child_id, tool_request, reason=reason)
            return result.model_copy(update={
                "operation_id": request.operation_id, "data": data,
            })

        return MCPSession(catalog, execute, instructions=(
            "This credential is limited to one delegated child task. Tools, paths, device "
            "and expiry are checked before each dispatch."
        ))
