"""Owner-scoped native AX sessions; never replay a dispatched input."""

import asyncio
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import Field, JsonValue, TypeAdapter

from .models import Contract

LIMIT = 64 * 1024
TIMEOUT = 5.0
JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class NativeApp(Contract):
    app: str = Field(min_length=1, max_length=512)


class NativeSession(Contract):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class NativeObserve(NativeSession, NativeApp):
    window_id: int = Field(strict=True, ge=1)


class NativeSetValue(NativeObserve):
    observation_id: str = Field(min_length=1, max_length=128)
    element_ref: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=8000)


class NativeGUIOutcomeUnknown(Exception):
    pass


def installed_helper() -> Path:
    if sys.platform != "darwin":
        raise ValueError("Native GUI requires macOS")
    root = Path(sys.prefix).parent
    helper = root / "native/anywhere-gui"
    manifest = root / "manifest.json"
    if (not helper.is_file() or helper.is_symlink() or not manifest.is_file()
            or not os.access(helper, os.X_OK)):
        raise ValueError("Verified native GUI helper is not installed")
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    if (not isinstance(recorded, dict) or not isinstance(recorded.get("files"), dict)
            or recorded["files"].get("native/anywhere-gui")
            != hashlib.sha256(helper.read_bytes()).hexdigest()):
        raise ValueError("Native GUI helper does not match its portable manifest")
    return helper


@dataclass
class NativeEntry:
    owner: str | None
    process: asyncio.subprocess.Process
    observations: set[str] = field(default_factory=set)


class NativeGUI:
    def __init__(self) -> None:
        self.entries: dict[str, NativeEntry] = {}
        # One desktop input stream, even when several authenticated owners connect.
        self.lock = asyncio.Lock()

    def _entry(self, session_id: str, owner: str | None) -> NativeEntry:
        entry = self.entries.get(session_id)
        if entry is None or entry.owner != owner:
            raise ValueError("Native GUI session unavailable")
        return entry

    async def _retire(self, session_id: str) -> None:
        entry = self.entries.pop(session_id, None)
        if entry is None:
            return
        process = entry.process
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 1)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def _call(self, session_id: str, request: dict[str, JsonValue], *,
                    mutation: bool = False) -> dict[str, JsonValue]:
        entry = self.entries[session_id]
        process = entry.process
        request_id = uuid.uuid4().hex
        wire = json.dumps({**request, "id": request_id}, ensure_ascii=False).encode() + b"\n"
        if len(wire) > LIMIT:
            raise ValueError("Native GUI request exceeds limit")
        dispatched = False
        try:
            async with asyncio.timeout(TIMEOUT):
                if (process.returncode is not None or process.stdin is None
                        or process.stdout is None):
                    raise ConnectionError("Native GUI helper is closed")
                dispatched = True
                process.stdin.write(wire)
                await process.stdin.drain()
                raw = await process.stdout.readline()
                if not raw.endswith(b"\n") or len(raw) > LIMIT:
                    raise ValueError("Invalid native GUI response framing")
                response = JSON_OBJECT.validate_json(raw)
                if response.get("id") != request_id:
                    raise ValueError("Native GUI response identity mismatch")
                result = response.get("result")
                if not isinstance(result, dict) or "error" in response:
                    error = response.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    # Never expose arbitrary helper diagnostics or exception text.
                    raise ValueError("Native GUI helper rejected request: " + (
                        code if isinstance(code, str) and code.replace("_", "").isalnum()
                        and len(code) <= 64 else "invalid_response"))
                return result
        except BaseException as error:
            await self._retire(session_id)
            if mutation and dispatched and isinstance(error, Exception):
                raise NativeGUIOutcomeUnknown(
                    "Native GUI input outcome unknown; inspect the target before any new input"
                ) from None
            raise

    async def windows(self, args: NativeApp, *, owner: str | None) -> dict[str, JsonValue]:
        async with self.lock:
            if len(self.entries) >= 8:
                raise ValueError("Close a native GUI session before opening another")
            helper = installed_helper()
            process = await asyncio.create_subprocess_exec(
                str(helper), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=LIMIT,
            )
            session_id = uuid.uuid4().hex
            self.entries[session_id] = NativeEntry(owner, process)
            result = await self._call(session_id, {"method": "windows", "app": args.app})
            return {**result, "session_id": session_id}

    async def observe(self, args: NativeObserve, *, owner: str | None) -> dict[str, JsonValue]:
        async with self.lock:
            entry = self._entry(args.session_id, owner)
            result = await self._call(args.session_id, {
                "method": "observe", "app": args.app, "window_id": args.window_id,
            })
            observation = result.get("observation_id")
            if not isinstance(observation, str) or not observation or len(observation) > 128:
                await self._retire(args.session_id)
                raise ValueError("Invalid native observation")
            if len(entry.observations) >= 128:
                entry.observations.clear()
            entry.observations.add(observation)
            return result

    async def set_value(self, args: NativeSetValue, *, owner: str | None
                        ) -> dict[str, JsonValue]:
        async with self.lock:
            entry = self._entry(args.session_id, owner)
            if args.observation_id not in entry.observations:
                raise ValueError("Native GUI observation unavailable; observe again")
            for current in self.entries.values():
                current.observations.clear()
            return await self._call(args.session_id, {
                "method": "set_value", "app": args.app, "window_id": args.window_id,
                "observation_id": args.observation_id, "element_ref": args.element_ref,
                "value": args.value,
            }, mutation=True)

    async def stop(self, args: NativeSession, *, owner: str | None) -> dict[str, JsonValue]:
        async with self.lock:
            self._entry(args.session_id, owner)
            await self._retire(args.session_id)
            return {"state": "closed"}

    async def close(self) -> None:
        async with self.lock:
            for session_id in list(self.entries):
                await self._retire(session_id)
