"""Private adapters for verified Subchat downloads and selected-device uploads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from .authorization import GrantIdentity
from .device_router import DeviceRouter
from .devices import DeviceStore
from .mcp_server import Catalog, Execute
from .models import Reply, Request
from .subchat_device_save import DeviceSave
from .subchat_gateway import LazySubchatGateway, subchat_ledger_owner

_UPLOAD_TOOLS = frozenset({"upload_begin", "upload_chunk", "upload_status",
                           "upload_commit", "operations_get"})


class SubchatSaveSource:
    def __init__(self, gateway: LazySubchatGateway,
                 current: Callable[[], GrantIdentity],
                 same_principal_grant: Callable[[GrantIdentity, str], bool]) -> None:
        self.gateway = gateway
        self.current = current
        self.same_principal_grant = same_principal_grant

    async def download(self, args: DeviceSave, offset: int,
                       limit: int) -> tuple[bytes, int, bool]:
        if limit < 1 or limit > 262_144:
            raise ValueError("Invalid internal download chunk limit")
        grant = self.current()
        if "subchat_save_file" not in grant.tools:
            raise PermissionError("Direct save is not granted")
        request = Request(operation_id=uuid.uuid4().hex, tool="subchat_download_file",
                          arguments={"operation_id": args.source_operation_id,
                                     "sandbox_link": args.sandbox_link,
                                     "max_bytes": limit, "offset": offset})
        owner = subchat_ledger_owner(grant, self.gateway.account_id)
        owner = self.gateway.owner_for_request(
            request, stable_owner=owner, legacy_grant_id=grant.grant_id,
            same_principal_grant=lambda candidate: self.same_principal_grant(grant, candidate),
        )
        reply = await self.gateway.execute(
            owner, request, frozenset({"subchat_download_file"}))
        if reply.state != "completed":
            raise ConnectionError("Verified Chat file chunk is unavailable")
        encoded = reply.data.get("content_base64")
        total = reply.data.get("file_size_bytes")
        eof = reply.data.get("eof")
        if (not isinstance(encoded, str) or not isinstance(total, int)
                or isinstance(total, bool) or not isinstance(eof, bool)
                or reply.data.get("offset") != offset):
            raise ValueError("Chat file chunk metadata is invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Chat file chunk encoding is invalid") from None
        if base64.b64encode(content).decode("ascii") != encoded:
            raise ValueError("Chat file chunk encoding is not canonical")
        return content, total, eof


class RoutedSaveTarget:
    """Target adapter with current OAuth and per-device catalog checks."""

    def __init__(self, directory: Path, catalog: Catalog, execute: Execute,
                 current: Callable[[], GrantIdentity], device_id: str) -> None:
        self.directory = directory
        self.catalog = catalog
        self.execute = execute
        self.current = current
        self.device_id = device_id

    def route_digest(self) -> str:
        route: dict[str, str | float | None]
        if self.device_id == "local":
            route = {"local_directory": str(self.directory.resolve())}
        else:
            store = DeviceStore(self.directory, read_only=True)
            try:
                device = store.get(self.device_id)
                route = cast(dict[str, str | float | None], {
                    key: device[key] for key in
                    ("device_id", "transport", "ssh_host", "resource", "client", "profile")
                })
            finally:
                store.close()
        return hashlib.sha256(json.dumps(
            route, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()

    def _grant(self) -> GrantIdentity:
        grant = self.current()
        if "subchat_save_file" not in grant.tools:
            raise PermissionError("Direct save is not granted")
        if self.device_id == "local":
            if not _UPLOAD_TOOLS <= grant.tools:
                raise PermissionError("Local upload tools are not granted")
        elif "devices_call" not in grant.tools:
            raise PermissionError("Device routing is not granted")
        return grant

    async def authorize(self) -> str:
        """Check the current route and all target upload capabilities."""
        self._grant()
        digest = self.route_digest()
        router = DeviceRouter(self.directory, self.catalog, self.execute)
        try:
            reply = await router.execute(Request(
                operation_id=uuid.uuid4().hex, tool="devices_tools",
                arguments={"device_id": self.device_id, "summary": True},
            ))
            tools = reply.data.get("tools")
            available = {item.get("name") for item in tools if isinstance(item, dict)} if (
                isinstance(tools, list)) else set()
            if reply.state != "completed" or not _UPLOAD_TOOLS <= available:
                raise PermissionError("Selected device upload tools are unavailable")
            self._grant()
            if self.route_digest() != digest:
                raise PermissionError("Selected device route changed during authorization")
            return digest
        finally:
            router.close()

    async def _call(self, name: str, arguments: dict[str, JsonValue], *,
                    operation_id: str | None = None) -> Reply:
        self._grant()
        if name not in _UPLOAD_TOOLS:
            raise ValueError("Unsupported save target tool")
        digest = self.route_digest()
        router = DeviceRouter(self.directory, self.catalog, self.execute)
        try:
            checked = await router.execute(Request(
                operation_id=uuid.uuid4().hex, tool="devices_tools",
                arguments={"device_id": self.device_id, "name": name},
            ))
            tools = checked.data.get("tools")
            if (checked.state != "completed" or not isinstance(tools, list)
                    or not any(isinstance(tool, dict) and tool.get("name") == name
                               for tool in tools)):
                raise PermissionError("Selected device upload tool is unavailable")
            self._grant()
            if self.route_digest() != digest:
                raise PermissionError("Selected device route changed before upload dispatch")
            routed = await router.execute(Request(
                operation_id=operation_id or uuid.uuid4().hex, tool="devices_call",
                arguments={"device_id": self.device_id, "tool": name,
                           "arguments": arguments},
            ))
            return routed
        finally:
            router.close()

    async def status(self, device_id: str, transfer_id: str) -> dict[str, object] | None:
        if device_id != self.device_id:
            raise ValueError("Selected device changed")
        reply = await self._call("upload_status", {"transfer_id": transfer_id})
        if reply.state == "failed":
            # The target deliberately withholds internal failure details. A
            # missing transfer and an unavailable status look alike; neither
            # authorizes another write after a dispatch checkpoint.
            return None
        if reply.state != "completed":
            raise ConnectionError("Selected device upload status is unavailable")
        result = reply.data.get("result")
        if not isinstance(result, dict):
            raise ValueError("Selected device upload status is invalid")
        return cast(dict[str, object], result)

    async def operation(self, device_id: str, operation_id: str
                        ) -> dict[str, object] | None:
        if device_id != self.device_id:
            raise ValueError("Selected device changed")
        reply = await self._call("operations_get", {"operation_id": operation_id})
        if reply.state != "completed":
            return None
        result = reply.data.get("result")
        return cast(dict[str, object], result) if isinstance(result, dict) else None

    async def _write(self, name: str, device_id: str, operation_id: str,
                     arguments: dict[str, JsonValue]) -> None:
        if device_id != self.device_id:
            raise ValueError("Selected device changed")
        reply = await self._call(name, arguments, operation_id=operation_id)
        if reply.state != "completed":
            raise ConnectionError("Selected device write outcome is unconfirmed")

    async def begin(self, device_id: str, operation_id: str, transfer_id: str,
                    path: str, total: int, sha256: str) -> None:
        await self._write("upload_begin", device_id, operation_id,
                          {"transfer_id": transfer_id, "path": path,
                           "total_bytes": total, "sha256": sha256})

    async def chunk(self, device_id: str, operation_id: str, transfer_id: str,
                    offset: int, content: bytes) -> None:
        await self._write("upload_chunk", device_id, operation_id,
                          {"transfer_id": transfer_id, "offset": offset,
                           "data_base64": base64.b64encode(content).decode("ascii")})

    async def commit(self, device_id: str, operation_id: str,
                     transfer_id: str) -> None:
        await self._write("upload_commit", device_id, operation_id,
                          {"transfer_id": transfer_id})
