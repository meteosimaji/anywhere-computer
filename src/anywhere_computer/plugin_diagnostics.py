"""Content-free, bounded diagnostics for untrusted plugin runtime output.

Free-form messages, stderr, and RPC data can contain credentials. Retain only
fixed classifications and protocol codes, never regex-redacted raw text.
Snapshots are persisted with the operation reply in the existing local ledger.
"""

from collections import deque

from pydantic import JsonValue

STDERR_CHUNK = 4096
MAX_EVENTS = 32
_MESSAGES = {
    "sender process is not authenticated": "sender_process_not_authenticated",
    "missing required codex turn metadata": "turn_metadata_required",
    "computer use is not active": "computer_use_inactive",
    "method not found": "method_not_found",
    "invalid params": "invalid_params",
    "unauthorized": "authentication_required",
    "permission denied": "permission_denied",
    "connection refused": "connection_refused",
    "timed out": "timeout",
}


def message_kind(value: object) -> str:
    if isinstance(value, str):
        lowered = value[:STDERR_CHUNK].casefold()
        for text, kind in _MESSAGES.items():
            if text in lowered:
                return kind
    return "redacted"


def rpc_diagnostic(value: object) -> dict[str, JsonValue]:
    raw = value if isinstance(value, dict) else {}
    code = raw.get("code")
    return {
        "rpc_code": code if type(code) is int and -(2**31) <= code < 2**31 else None,
        "message_kind": message_kind(raw.get("message")),
        "data_present": "data" in raw,
        "content_redacted": True,
    }


class PluginRPCError(RuntimeError):
    def __init__(self, error: object) -> None:
        self.details = rpc_diagnostic(error)
        super().__init__("Codex app server rejected the plugin request")


class PluginDiagnostics:
    def __init__(self) -> None:
        self.events: deque[JsonValue] = deque(maxlen=MAX_EVENTS)
        self.bytes_seen = 0
        self.chunks_seen = 0
        self.capture_failed = False

    def feed(self, chunk: bytes) -> None:
        self.bytes_seen = min(2**63 - 1, self.bytes_seen + len(chunk))
        self.chunks_seen = min(2**63 - 1, self.chunks_seen + 1)
        self.events.append({"message_kind": message_kind(chunk.decode("utf-8", errors="replace"))})

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "stderr_bytes_seen": self.bytes_seen, "stderr_chunks_seen": self.chunks_seen,
            "stderr_events": list(self.events), "content_redacted": True,
            "events_dropped": self.chunks_seen > MAX_EVENTS,
            "capture_failed": self.capture_failed,
        }


def failure_diagnostic(error: BaseException, stage: str) -> dict[str, JsonValue]:
    if isinstance(error, PluginRPCError):
        kind = "rpc_error"
    elif isinstance(error, TimeoutError):
        kind = "timeout"
    elif isinstance(error, ConnectionError):
        kind = "connection_error"
    elif isinstance(error, OSError):
        kind = "runtime_error"
    elif isinstance(error, (ValueError, TypeError)):
        kind = "invalid_response"
    else:
        kind = "runtime_error"
    result: dict[str, JsonValue] = {"failure_stage": stage, "failure_kind": kind}
    if isinstance(error, PluginRPCError):
        result.update(error.details)
    return result
