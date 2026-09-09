"""Local connector routing; never mounted in a remotely authorized engine."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from pydantic import Field, JsonValue

from .client_tokens import ClientTokens
from .devices import DeviceData, DeviceStore
from .http_client import HTTPBackend
from .mcp_server import Catalog, Execute
from .models import Contract, Empty, Reply, Request
from .ssh_client import SSHBackend

ROUTER_TOOLS = frozenset({"devices_list", "devices_tools", "devices_call"})


class DeviceTarget(Contract):
    device_id: str = Field(min_length=1, max_length=32, pattern=r"^(local|[a-f0-9]{32})$")


class DeviceCall(DeviceTarget):
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class DeviceBackend(Protocol):
    async def catalog(self) -> list[JsonValue]: ...
    async def execute(self, request: Request) -> Reply: ...
    async def close(self) -> None: ...


class DeviceRouter:
    def __init__(
        self, directory: Path, catalog: Catalog, execute: Execute,
        *, backend_factory: Callable[[DeviceData], DeviceBackend] | None = None,
    ) -> None:
        self.store = DeviceStore(directory)
        self.local_catalog = catalog
        self.local_execute = execute
        self.backend_factory = backend_factory or self._backend
        # A digest only: no argument contents, paths, tokens or responses are persisted here.
        with self.store.db:
            self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS routed_operations ("
                "id TEXT PRIMARY KEY, digest TEXT NOT NULL, dispatched INTEGER NOT NULL DEFAULT 0)"
            )

    def _claim_dispatch(self, operation_id: str) -> bool:
        with self.store.db:
            changed = self.store.db.execute(
                "UPDATE routed_operations SET dispatched=1 WHERE id=? AND dispatched=0",
                (operation_id,),
            )
            return changed.rowcount == 1

    def _backend(self, device: DeviceData) -> DeviceBackend:
        if device["transport"] == "ssh":
            return SSHBackend(str(device["ssh_host"]))
        return HTTPBackend(ClientTokens(
            self.store.directory, resource=str(device["resource"]),
            client=str(device["client_id"]), profile=str(device["profile"]),
        ))

    def _bind(self, request: Request, device: DeviceData | None) -> None:
        digest = hashlib.sha256(json.dumps(
            {"tool": request.tool, "arguments": request.arguments, "route": (
                {key: device[key] for key in
                 ("device_id", "transport", "ssh_host", "resource", "client_id", "profile")}
                if device is not None else {"local_directory": str(self.store.directory)}
            )},
            sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode()).hexdigest()
        with self.store.db:
            self.store.db.execute(
                "INSERT OR IGNORE INTO routed_operations(id,digest) VALUES (?,?)",
                (request.operation_id, digest),
            )
            found = self.store.db.execute(
                "SELECT digest FROM routed_operations WHERE id=?", (request.operation_id,),
            ).fetchone()
            if found is None or found[0] != digest:
                raise ValueError("Operation ID is already bound to another device or request")

    @staticmethod
    def _remote_tools(tools: list[JsonValue]) -> list[JsonValue]:
        names: set[str] = set()
        visible: list[JsonValue] = []
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise ValueError("Remote tool catalog is invalid")
            name = cast(str, tool["name"])
            if name in names:
                raise ValueError("Remote tool catalog contains duplicate names")
            names.add(name)
            if not name.startswith(("devices_", "connection_setup_", "__")):
                visible.append(tool)
        return visible

    async def catalog(self) -> list[JsonValue]:
        local = await self.local_catalog()
        if any(isinstance(item, dict) and item.get("name") in ROUTER_TOOLS for item in local):
            raise ValueError("Device router tool name collides with the local engine")
        descriptions: list[tuple[str, str, type[Contract], bool]] = [
            ("devices_list", "List locally registered devices and cached observations. "
             "No connection check is performed; local refers to this connector's computer.",
             Empty, True),
            ("devices_tools", "Fetch the current authorized tool schemas from an explicit "
             "device_id. Use this before devices_call. Registration/login use the local CLI.",
             DeviceTarget, True),
            ("devices_call", "Execute a tool on an explicit device_id using its saved SSH/HTTP "
             "authorization. May write files or run commands. Nested device routing is forbidden. "
             "On response loss, query operations_get on the SAME device with the operation_id; "
             "never repeat a write with a new ID.", DeviceCall, False),
        ]
        extra: list[JsonValue] = [
            {
                "name": name, "description": description, "inputSchema": model.model_json_schema(),
                "outputSchema": Reply.model_json_schema(),
                "annotations": {
                    "readOnlyHint": read_only, "destructiveHint": not read_only,
                    "idempotentHint": read_only, "openWorldHint": name != "devices_list",
                },
            }
            for name, description, model, read_only in descriptions
        ]
        return [*local, *extra]

    async def execute(self, request: Request) -> Reply:
        if request.tool not in ROUTER_TOOLS:
            return await self.local_execute(request)
        dispatched = False
        target: str | None = None
        backend: DeviceBackend | None = None
        try:
            if request.tool == "devices_list":
                Empty.model_validate(request.arguments)
                self._bind(request, None)
                devices: list[JsonValue] = [{
                    "device_id": "local", "name": "This computer", "transport": "local",
                    "state": "unknown",
                }]
                devices.extend(cast(JsonValue, device) for device in self.store.list())
                return Reply(operation_id=request.operation_id, state="completed",
                             data={"devices": devices})
            args = (DeviceCall.model_validate(request.arguments) if request.tool == "devices_call"
                    else DeviceTarget.model_validate(request.arguments))
            target = args.device_id
            if isinstance(args, DeviceCall) and args.tool.startswith(
                ("devices_", "connection_setup_", "__")
            ):
                raise ValueError("Nested device routing is not allowed")
            # Read the saved endpoint for each call; removal or re-registration cannot reuse an ID.
            device = self.store.get(args.device_id) if args.device_id != "local" else None
            self._bind(request, device)
            if device is not None:
                backend = self.backend_factory(device)
            tools = self._remote_tools(
                await backend.catalog() if backend is not None else await self.local_catalog()
            )
            if not isinstance(args, DeviceCall):
                return Reply(operation_id=request.operation_id, state="completed",
                             data={"device_id": args.device_id, "tools": tools})
            if not any(isinstance(tool, dict) and tool.get("name") == args.tool for tool in tools):
                raise ValueError("Tool is not available in this device's authorized catalog")
            if not self._claim_dispatch(request.operation_id):
                return Reply(
                    operation_id=request.operation_id, state="unknown",
                    data={"device_id": args.device_id, "previously_forwarded": True},
                    error="This operation was already forwarded and was not sent again. "
                    "Query operations_get on the SAME device using a fresh lookup operation ID.",
                )
            forwarded = Request(operation_id=request.operation_id, tool=args.tool,
                                arguments=args.arguments)
            dispatched = True
            reply = (await backend.execute(forwarded) if backend is not None
                     else await self.local_execute(forwarded))
            if reply.operation_id != request.operation_id:
                raise ConnectionError("Device operation ID mismatch")
            return reply.model_copy(update={"data": {
                "device_id": args.device_id, "result": reply.data,
            }})
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return Reply(
                operation_id=request.operation_id, state="unknown" if dispatched else "failed",
                data={"device_id": target} if target is not None else {},
                error=("Device response was not confirmed. Query operations_get on the SAME "
                       "device with this operation_id; do not repeat the write." if dispatched else
                       "Device request was not dispatched. Check the registered device, "
                       "authorization, tool schema and operation ID binding."),
            )
        finally:
            if backend is not None:
                try:
                    await backend.close()
                except (OSError, ValueError, RuntimeError):
                    pass  # Cleanup cannot change a confirmed operation outcome.

    def close(self) -> None:
        self.store.close()
