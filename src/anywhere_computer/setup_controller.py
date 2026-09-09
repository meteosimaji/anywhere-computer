"""Local setup-screen state; no network endpoint or credential handling."""

import asyncio
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .http_service import HTTPServiceConfig, load_http_config, save_http_config


class SetupProgress(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    phase: Literal["new", "review", "saving", "configured", "conflict", "invalid"]
    plan_id: str | None = None
    configuration: HTTPServiceConfig | None = None
    error: str | None = None


class SetupController:
    """A trusted native host owns one controller; progress contains public fields only."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._plan: HTTPServiceConfig | None = None
        self._plan_id: str | None = None
        self._saving = False
        self._lock = asyncio.Lock()

    def progress(self) -> SetupProgress:
        destination = self.directory / "http-server"
        try:
            destination.lstat()
        except FileNotFoundError:
            return SetupProgress(
                phase="saving" if self._saving else "review" if self._plan else "new",
                plan_id=self._plan_id, configuration=self._plan,
            )
        except OSError:
            return SetupProgress(phase="invalid", error="Saved setup could not be inspected")
        try:
            saved = load_http_config(self.directory)
        except ValueError:
            return SetupProgress(phase="invalid", error="Saved setup could not be verified")
        return SetupProgress(
            phase="conflict" if self._plan is not None and saved != self._plan else "configured",
            plan_id=self._plan_id, configuration=saved,
        )

    def review(self, plan: HTTPServiceConfig) -> SetupProgress:
        if self._saving:
            raise RuntimeError("Wait for the pending configuration save")
        # Revalidate public serialized data rather than trusting model_construct/copy.
        validated = HTTPServiceConfig.model_validate_json(plan.model_dump_json())
        self._plan = validated
        self._plan_id = hashlib.sha256(validated.model_dump_json().encode()).hexdigest()
        return self.progress()

    async def confirm(self, plan_id: str) -> SetupProgress:
        async with self._lock:
            if self._plan is None or plan_id != self._plan_id:
                raise ValueError("The displayed setup plan is no longer current")
            observed = self.progress()
            if observed.phase != "review":
                return observed
            self._saving = True
            try:
                await save_http_config(self.directory, self._plan)
            except (OSError, ValueError, RuntimeError):
                # Publication can succeed before the caller loses its result. Reconcile
                # actual disk state before describing failure or allowing another save.
                observed = self.progress()
                if observed.phase == "saving":
                    return SetupProgress(
                        phase="review", plan_id=self._plan_id, configuration=self._plan,
                        error="Configuration was not published; review and try again",
                    )
                return observed
            finally:
                self._saving = False
            return self.progress()
