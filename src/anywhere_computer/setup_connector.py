"""Trusted local MCP setup actions; not installed in the remote engine."""

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, JsonValue, model_validator

from .mcp_server import Catalog, Execute
from .models import Contract, Empty, Reply, Request
from .remote_setup import CHATGPT_CLIENT, CHATGPT_REDIRECT, plan_remote_setup
from .setup_controller import SetupController
from .state import prepare_directory

SETUP_TOOLS = frozenset({
    "connection_setup_status", "connection_setup_plan", "connection_setup_confirm",
})


class ConnectionSetupDraft(Contract):
    resource: str = Field(max_length=2048)
    client_kind: Literal["native", "chatgpt"] = Field(
        default="native",
        description="Use chatgpt to set its OAuth client and callback automatically",
    )
    mode: Literal["read-only", "files", "all"] = "read-only"
    owner: str = Field(default="owner", min_length=1, max_length=128)
    client: str = Field(default="anywhere-native", min_length=1, max_length=128)
    port: int = Field(default=8768, ge=1, le=65535)
    redirects: list[str] | None = Field(default=None, min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_chatgpt_preset(self) -> Self:
        if self.client_kind == "chatgpt":
            if "client" in self.model_fields_set and self.client != CHATGPT_CLIENT:
                raise ValueError("ChatGPT preset conflicts with the supplied client identifier")
            if self.redirects is not None and self.redirects != [CHATGPT_REDIRECT]:
                raise ValueError("ChatGPT preset conflicts with the supplied callback")
        return self


class ConnectionSetupConfirmation(Contract):
    plan_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class SetupConnector:
    def __init__(self, directory: Path, catalog: Catalog, execute: Execute) -> None:
        prepare_directory(directory)
        self.controller = SetupController(directory)
        self.local_catalog = catalog
        self.local_execute = execute
        self.db = sqlite3.connect(directory / "setup-operations.sqlite3", timeout=10)
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS setup_operations ("
                "id TEXT PRIMARY KEY, digest TEXT NOT NULL, dispatched INTEGER NOT NULL, "
                "reply TEXT)"
            )
        # Besides serializing the controller, this makes cancellation boundaries explicit:
        # once a save is claimed, a retry can only reconcile its status.
        self._lock = asyncio.Lock()
        self._plan_operation: str | None = None

    @staticmethod
    def _digest(request: Request) -> str:
        payload = json.dumps(
            {"tool": request.tool, "arguments": request.arguments},
            sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _bind(self, request: Request) -> None:
        digest = self._digest(request)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO setup_operations(id,digest,dispatched) VALUES (?,?,0)",
                (request.operation_id, digest),
            )
            row = self.db.execute(
                "SELECT digest FROM setup_operations WHERE id=?", (request.operation_id,),
            ).fetchone()
            if row is None or row[0] != digest:
                raise ValueError("Operation ID is already bound to another connector request")

    def _claim(self, request: Request) -> Reply | None:
        with self.db:
            claimed = self.db.execute(
                "UPDATE setup_operations SET dispatched=1 WHERE id=? AND dispatched=0",
                (request.operation_id,),
            ).rowcount
            if claimed == 1:
                return None
            row = self.db.execute(
                "SELECT reply FROM setup_operations WHERE id=?", (request.operation_id,),
            ).fetchone()
            if row is None:
                raise sqlite3.Error("Setup operation binding was not created")
            if row[0] is not None:
                if request.tool != "connection_setup_plan":
                    return Reply.model_validate_json(str(row[0]))
                recorded = Reply.model_validate_json(str(row[0]))
                current = self.controller.progress()
                if (request.operation_id == self._plan_operation and current.phase == "review"
                        and current.plan_id == recorded.data.get("plan_id")):
                    return recorded
            return Reply(
                operation_id=request.operation_id, state="unknown",
                error="Setup operation was already started and was not repeated. "
                "Query connection_setup_status to inspect current state before continuing.",
            )

    def _save_reply(self, request: Request, reply: Reply) -> None:
        with self.db:
            self.db.execute(
                "UPDATE setup_operations SET reply=? WHERE id=?",
                (reply.model_dump_json(), request.operation_id),
            )

    async def catalog(self) -> list[JsonValue]:
        original = await self.local_catalog()
        if any(isinstance(tool, dict) and tool.get("name") in SETUP_TOOLS for tool in original):
            raise ValueError("Setup tool collides with a local tool")
        definitions: list[tuple[str, str, type[Contract], bool]] = [
            ("connection_setup_status", "Inspect the local connector's HTTP setup stage. "
             "Configured means configuration saved, not credentials or connectivity verified.",
             Empty, True),
            ("connection_setup_plan", "Prepare the local connector's initial HTTP configuration "
             "for review. Choose client_kind=chatgpt for automatic ChatGPT OAuth settings. "
             "Public fields only; no passwords/tokens, service start or OS changes.",
             ConnectionSetupDraft, False),
            ("connection_setup_confirm", "Save the exact local HTTP configuration plan reviewed "
             "by the user. Never overwrites existing configuration. On response loss call "
             "connection_setup_status; do not use operations_get for this connector-only action.",
             ConnectionSetupConfirmation, False),
        ]
        extra: list[JsonValue] = [cast(JsonValue, {
            "name": name, "description": description, "inputSchema": schema.model_json_schema(),
            "outputSchema": Reply.model_json_schema(), "annotations": {
                "readOnlyHint": read_only, "destructiveHint": False,
                "idempotentHint": name != "connection_setup_plan", "openWorldHint": False,
            },
        }) for name, description, schema, read_only in definitions]
        return [*original, *extra]

    async def execute(self, request: Request) -> Reply:
        try:
            # Bind the outer request namespace before delegation, so a setup operation
            # ID cannot also dispatch a file/terminal/device operation (or vice versa).
            self._bind(request)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return Reply(operation_id=request.operation_id, state="failed",
                         error="Connector operation ID could not be bound to this request.")
        if request.tool not in SETUP_TOOLS:
            return await self.local_execute(request)
        async with self._lock:
            try:
                # Validate before dispatch. A corrected request requires a fresh ID.
                if request.tool == "connection_setup_status":
                    Empty.model_validate(request.arguments)
                elif request.tool == "connection_setup_plan":
                    args = ConnectionSetupDraft.model_validate(request.arguments)
                else:
                    confirmation = ConnectionSetupConfirmation.model_validate(request.arguments)
                replay = self._claim(request)
                if replay is not None:
                    return replay
                if request.tool == "connection_setup_status":
                    progress = self.controller.progress()
                elif request.tool == "connection_setup_plan":
                    plan = await plan_remote_setup(
                        resource=args.resource, owner=args.owner,
                        client=CHATGPT_CLIENT if args.client_kind == "chatgpt" else args.client,
                        port=args.port, mode=args.mode,
                        redirects=(frozenset({CHATGPT_REDIRECT})
                                   if args.client_kind == "chatgpt" else frozenset(args.redirects)
                                   if args.redirects is not None else None),
                    )
                    progress = self.controller.review(plan)
                else:
                    progress = await self.controller.confirm(confirmation.plan_id)
                reply = Reply(operation_id=request.operation_id, state="completed",
                              data=cast(dict[str, JsonValue], progress.model_dump(mode="json")))
                self._save_reply(request, reply)
                if request.tool == "connection_setup_plan":
                    self._plan_operation = request.operation_id
                return reply
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                return Reply(operation_id=request.operation_id, state="failed",
                             error="Setup could not be completed. Check the public configuration "
                             "fields and reload connection_setup_status to inspect saved state.")

    def close(self) -> None:
        self.db.close()
