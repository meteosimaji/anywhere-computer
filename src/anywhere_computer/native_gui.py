"""Owner-scoped native AX sessions; never replay a dispatched input."""

import asyncio
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from .models import Contract
from .plugin_images import IMAGE_LIMIT, bounded_image

LIMIT = 64 * 1024
VISUAL_LIMIT = 4 * 1024 * 1024
TIMEOUT = 5.0
JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
HELPER_ERROR_CODES = frozenset({
    "accessibility_required", "ambiguous_process", "ax_error", "deadline_exceeded",
    "element_not_enabled", "element_unavailable", "input_too_large", "internal_error",
    "internal_state", "invalid_input", "invalid_json", "observation_limit_exceeded",
    "observation_mismatch", "observation_unavailable", "press_target_changed",
    "process_identity_changed", "process_identity_unavailable", "process_not_found",
    "response_too_large", "stdin_error", "tree_limit_exceeded", "unknown_method",
    "target_ambiguous", "target_changed", "target_not_found", "value_changed",
    "value_not_comparable", "value_not_settable",
    "window_limit_exceeded", "window_unavailable",
    "action_not_observed", "action_target_changed", "screen_recording_required",
    "screen_capture_unavailable", "capture_window_unavailable", "capture_window_ambiguous",
    "capture_target_changed", "capture_failed", "image_too_large",
})
# The helper reports these only before AXPress/AXValue is attempted. A valid
# response leaves its JSON-lines stream usable, even when the target is stale.
NONFATAL_HELPER_ERRORS = frozenset({
    "accessibility_required", "ambiguous_process", "element_not_enabled",
    "element_unavailable", "invalid_input", "observation_limit_exceeded",
    "observation_mismatch", "observation_unavailable", "press_target_changed",
    "process_identity_changed", "process_identity_unavailable", "process_not_found",
    "target_ambiguous", "target_changed", "target_not_found", "tree_limit_exceeded",
    "value_changed", "value_not_comparable",
    "value_not_settable", "window_limit_exceeded", "window_unavailable",
    "action_not_observed", "action_target_changed", "screen_recording_required",
    "screen_capture_unavailable", "capture_window_unavailable", "capture_window_ambiguous",
    "capture_target_changed", "capture_failed", "image_too_large",
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


class NativeWindow(NativeSession, NativeApp):
    window_id: int = Field(strict=True, ge=1)


class NativeObserve(NativeWindow):
    include_image: bool = Field(default=False, exclude=True, description=(
        "Include a bounded screenshot of this exact window with pixel dimensions and global "
        "point bounds. Requires existing Screen Recording permission; does not request it."
    ))
    compact: bool = Field(default=False, exclude=True, description=(
        "Return only actionable role/label/identifier targets, omitting the full AX tree. "
        "Use the full tree when a target is missing or ambiguous."
    ))


class NativePress(NativeWindow):
    observation_id: str = Field(min_length=1, max_length=128)
    element_ref: str = Field(min_length=1, max_length=128)


class NativeSetValue(NativePress):
    value: str = Field(max_length=8000)


class NativeAction(NativePress):
    action: Literal[
        "AXPress", "AXIncrement", "AXDecrement", "AXConfirm", "AXCancel", "AXShowMenu",
        "AXPick", "AXScrollUpByPage", "AXScrollDownByPage", "AXScrollLeftByPage",
        "AXScrollRightByPage",
    ] = Field(description="Copy one action from the selected element's observed actions list.")


class NativeTarget(NativeWindow):
    observation_id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, min_length=1, max_length=512)
    identifier: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def require_name(self) -> "NativeTarget":
        if self.label is None and self.identifier is None:
            raise ValueError("Choose an observed label or identifier")
        return self


class NativePressTarget(NativeTarget):
    pass


class NativeSetValueTarget(NativeTarget):
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
                response_limit = VISUAL_LIMIT if request.get("include_image") is True else LIMIT
                if not raw.endswith(b"\n") or len(raw) > response_limit:
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
                    if keep_session and (code in {
                            "value_changed", "target_changed", "action_target_changed"} or (
                            code == "press_target_changed"
                            and request.get("method") in {"press", "press_target"})):
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
                stderr=asyncio.subprocess.DEVNULL, limit=VISUAL_LIMIT,
            )
            session_id = uuid.uuid4().hex
            self.entries[session_id] = NativeEntry(owner, process)
            result = await self._call(session_id, {"method": "windows", "app": args.app})
            return {**result, "session_id": session_id}

    async def observe(self, args: NativeObserve, *, owner: str | None) -> dict[str, JsonValue]:
        async with self.lock:
            entry = self._entry(args.session_id, owner)
            result = await self._call(args.session_id, {
                "method": "observe_targets" if args.compact else "observe",
                "app": args.app, "window_id": args.window_id,
                **({"include_image": True} if args.include_image else {}),
            })
            visual_error = result.get("visual_unavailable")
            if (args.include_image and "content" not in result
                    and (not isinstance(visual_error, str)
                         or visual_error not in HELPER_ERROR_CODES)):
                await self._retire(args.session_id)
                raise ValueError("Invalid native observation")
            if args.include_image and "content" in result:
                content = result["content"]
                try:
                    if not isinstance(content, list) or len(content) != 1:
                        raise ValueError("Invalid native observation")
                    item = content[0]
                    if not isinstance(item, dict) or item.get("type") != "image":
                        raise ValueError("Invalid native observation")
                    picture, _ = bounded_image(item, IMAGE_LIMIT)
                    if picture is None:
                        raise ValueError("Invalid native observation")
                    result["content"] = [picture]
                except ValueError:
                    await self._retire(args.session_id)
                    raise ValueError("Invalid native observation") from None
            observation = result.get("observation_id")
            if not isinstance(observation, str) or not observation or len(observation) > 128:
                await self._retire(args.session_id)
                raise ValueError("Invalid native observation")
            if len(entry.observations) >= 128:
                entry.observations.clear()
            entry.observations.add(observation)
            return result

    async def _apply(self, session_id: str, observation_id: str,
                     request: dict[str, JsonValue], *, owner: str | None
                     ) -> dict[str, JsonValue]:
        async with self.lock:
            entry = self._entry(session_id, owner)
            if observation_id not in entry.observations:
                raise ValueError("Native GUI observation unavailable; observe again")
            for current in self.entries.values():
                current.observations.clear()
            return await self._call(session_id, request, mutation=True)

    async def set_value(self, args: NativeSetValue, *, owner: str | None
                        ) -> dict[str, JsonValue]:
        return await self._apply(args.session_id, args.observation_id, {
                "method": "set_value", "app": args.app, "window_id": args.window_id,
                "observation_id": args.observation_id, "element_ref": args.element_ref,
                "value": args.value,
            }, owner=owner)

    async def press(self, args: NativePress, *, owner: str | None) -> dict[str, JsonValue]:
        return await self._apply(args.session_id, args.observation_id, {
                "method": "press", "app": args.app, "window_id": args.window_id,
                "observation_id": args.observation_id, "element_ref": args.element_ref,
            }, owner=owner)

    async def action(self, args: NativeAction, *, owner: str | None) -> dict[str, JsonValue]:
        return await self._apply(args.session_id, args.observation_id, {
            "method": "action", "app": args.app, "window_id": args.window_id,
            "observation_id": args.observation_id, "element_ref": args.element_ref,
            "action": args.action,
        }, owner=owner)

    async def set_value_target(self, args: NativeSetValueTarget, *, owner: str | None
                               ) -> dict[str, JsonValue]:
        return await self._apply(args.session_id, args.observation_id, {
            "method": "set_value_target", "app": args.app, "window_id": args.window_id,
            "observation_id": args.observation_id, "role": args.role,
            **({"label": args.label} if args.label is not None else {}),
            **({"identifier": args.identifier} if args.identifier is not None else {}),
            "value": args.value,
        }, owner=owner)

    async def press_target(self, args: NativePressTarget, *, owner: str | None
                           ) -> dict[str, JsonValue]:
        return await self._apply(args.session_id, args.observation_id, {
            "method": "press_target", "app": args.app, "window_id": args.window_id,
            "observation_id": args.observation_id, "role": args.role,
            **({"label": args.label} if args.label is not None else {}),
            **({"identifier": args.identifier} if args.identifier is not None else {}),
        }, owner=owner)

    async def stop(self, args: NativeSession, *, owner: str | None) -> dict[str, JsonValue]:
        async with self.lock:
            self._entry(args.session_id, owner)
            await self._retire(args.session_id)
            return {"state": "closed"}

    async def close(self) -> None:
        async with self.lock:
            for session_id in list(self.entries):
                await self._retire(session_id)
