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
HELPER_ERROR_CODES = frozenset({
    "accessibility_required", "ambiguous_process", "ax_error", "deadline_exceeded",
    "element_not_enabled", "element_unavailable", "input_too_large", "internal_error",
    "internal_state", "invalid_input", "invalid_json", "observation_limit_exceeded",
    "observation_mismatch", "observation_unavailable", "press_target_changed",
    "process_identity_changed", "process_identity_unavailable", "process_not_found",
    "response_too_large", "stdin_error", "tree_limit_exceeded", "unknown_method",
    "value_changed", "value_not_comparable", "value_not_settable",
    "window_limit_exceeded", "window_unavailable",
})
# The helper reports these only before AXPress/AXValue is attempted. A valid
# response leaves its JSON-lines stream usable, even when the target is stale.
NONFATAL_HELPER_ERRORS = frozenset({
    "accessibility_required", "ambiguous_process", "element_not_enabled",
    "element_unavailable", "invalid_input", "observation_limit_exceeded",
    "observation_mismatch", "observation_unavailable", "press_target_changed",
    "process_identity_changed", "process_identity_unavailable", "process_not_found",
    "tree_limit_exceeded", "value_changed", "value_not_comparable",
    "value_not_settable", "window_limit_exceeded", "window_unavailable",
})
AX_DIAGNOSTIC_STAGES = frozenset({
    "attribute_type", "copy_actions", "copy_attribute", "copy_attribute_values",
    "count_attribute", "get_pid", "is_settable", "perform_action",
    "set_attribute", "set_timeout", "verify_pid",
})
AX_DIAGNOSTIC_ATTRIBUTES = frozenset({
    "AXChildren", "AXDescription", "AXEnabled", "AXHelp", "AXIdentifier",
    "AXRole", "AXTitle", "AXValue", "AXWindows", "other",
})
HELPER_REJECTION_PREFIX = "Native GUI helper rejected request: "


def helper_error_code(message: str) -> str | None:
    if not message.startswith(HELPER_REJECTION_PREFIX):
        return None
    code = message.removeprefix(HELPER_REJECTION_PREFIX).partition(" (")[0]
    return code if code in HELPER_ERROR_CODES | {"invalid_response"} else None


def _project_ax_diagnostic(error: dict[str, JsonValue]) -> str:
    fields = []
    stage = error.get("stage")
    attribute = error.get("attribute")
    status = error.get("ax_status")
    if isinstance(stage, str) and stage in AX_DIAGNOSTIC_STAGES:
        fields.append(f"stage={stage}")
    if isinstance(attribute, str) and attribute in AX_DIAGNOSTIC_ATTRIBUTES:
        fields.append(f"attribute={attribute}")
    if type(status) is int and -26000 <= status <= 0:
        fields.append(f"ax_status={status}")
    return " (" + ", ".join(fields) + ")" if fields else ""


class NativeApp(Contract):
    app: str = Field(min_length=1, max_length=512)


class NativeSession(Contract):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class NativeObserve(NativeSession, NativeApp):
    window_id: int = Field(strict=True, ge=1)


class NativePress(NativeObserve):
    observation_id: str = Field(min_length=1, max_length=128)
    element_ref: str = Field(min_length=1, max_length=128)


class NativeSetValue(NativePress):
    value: str = Field(max_length=8000)


class NativeGUIOutcomeUnknown(Exception):
    pass


class NativeGUIInputRefused(ValueError):
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
        keep_session = False
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
                try:
                    response = JSON_OBJECT.validate_json(raw)
                except ValueError:
                    # Pydantic's error includes input_value, which is untrusted helper output.
                    raise ValueError("Invalid native GUI response JSON") from None
                if response.get("id") != request_id:
                    raise ValueError("Native GUI response identity mismatch")
                result = response.get("result")
                if not isinstance(result, dict) or "error" in response:
                    error = response.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    keep_session = (isinstance(code, str)
                                    and request.get("method") != "windows"
                                    and "result" not in response
                                    and code in NONFATAL_HELPER_ERRORS)
                    if keep_session and (code == "value_changed" or (
                            code == "press_target_changed" and request.get("method") == "press")):
                        raise NativeGUIInputRefused(
                            "Native GUI target changed since observation; input was not attempted")
                    if keep_session and code == "value_not_comparable":
                        raise NativeGUIInputRefused(
                            "Native GUI value could not be compared; input was not attempted")
                    # Never expose arbitrary helper diagnostics or exception text.
                    safe_code = (code if isinstance(code, str) and code in HELPER_ERROR_CODES
                                 else "invalid_response")
                    detail = (_project_ax_diagnostic(error)
                              if safe_code == "ax_error" and isinstance(error, dict) else "")
                    raise ValueError(HELPER_REJECTION_PREFIX + safe_code + detail)
                return result
        except BaseException as error:
            if not keep_session:
                await self._retire(session_id)
            if (mutation and dispatched and not keep_session and isinstance(error, Exception)
                    and not isinstance(error, NativeGUIInputRefused)):
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

    async def press(self, args: NativePress, *, owner: str | None) -> dict[str, JsonValue]:
        async with self.lock:
            entry = self._entry(args.session_id, owner)
            if args.observation_id not in entry.observations:
                raise ValueError("Native GUI observation unavailable; observe again")
            for current in self.entries.values():
                current.observations.clear()
            return await self._call(args.session_id, {
                "method": "press", "app": args.app, "window_id": args.window_id,
                "observation_id": args.observation_id, "element_ref": args.element_ref,
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
