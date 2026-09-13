"""Credential-free RFC 8628 polling lifecycle for a single enrollment attempt.

The transport must call begin_request before sending and finish_request after
receiving a validated response. This component never issues or stores tokens,
and an authorized grant is not a completed device association.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

PollPhase = Literal["waiting", "requesting", "authorized", "denied", "expired",
                    "cancelled", "failed", "uncertain"]
PollOutcome = Literal["authorized", "authorization_pending", "slow_down", "access_denied",
                      "expired_token", "connection_timeout", "invalid_response", "unknown_result"]


@dataclass(frozen=True)
class PollProgress:
    phase: PollPhase
    retry_after: float


class DevicePolling:
    """Owned by one controller/event loop; no concurrent transport requests."""

    def __init__(
        self, expires_in: int, interval: int = 5, *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(expires_in) is not int or expires_in <= 0:
            raise ValueError("Device authorization lifetime must be a positive integer")
        if type(interval) is not int or interval <= 0:
            raise ValueError("Device polling interval must be a positive integer")
        self._clock = clock
        now = self._now()
        self._deadline = now + expires_in
        self._interval = interval
        self._next = now + interval
        self._phase: PollPhase = "waiting"

    def _now(self) -> float:
        now = self._clock()
        if not math.isfinite(now):
            raise ValueError("Polling clock must be finite")
        return now

    def progress(self) -> PollProgress:
        now = self._now()
        if self._phase == "waiting" and now >= self._deadline:
            self._phase = "expired"
        return PollProgress(
            self._phase, max(0.0, min(self._next, self._deadline) - now)
            if self._phase == "waiting" else 0.0,
        )

    def begin_request(self) -> bool:
        state = self.progress()
        if state.phase != "waiting" or state.retry_after > 0:
            return False
        self._phase = "requesting"
        return True

    def cancel(self) -> PollProgress:
        if self._phase in {"waiting", "requesting"}:
            self._phase = "cancelled"
        return self.progress()

    def finish_request(self, outcome: PollOutcome) -> PollProgress:
        # A late response cannot revive cancellation or an already consumed grant.
        if self._phase != "requesting":
            return self.progress()
        now = self._now()
        if outcome == "authorized":
            self._phase = "authorized"
        elif outcome == "unknown_result":
            self._phase = "uncertain"
        elif outcome == "access_denied":
            self._phase = "denied"
        elif outcome == "expired_token":
            self._phase = "expired"
        elif outcome in {"authorization_pending", "slow_down", "connection_timeout"}:
            if outcome == "slow_down":
                self._interval += 5
            elif outcome == "connection_timeout":
                self._interval *= 2
            self._next = now + self._interval
            self._phase = "waiting"
        else:
            # Includes unknown errors: never turn terminal denial into polling.
            self._phase = "failed"
        return self.progress()
