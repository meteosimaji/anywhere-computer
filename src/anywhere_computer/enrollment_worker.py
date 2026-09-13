"""Fixed-command enrollment worker for a trusted native host.

The native host owns this worker for the duration of an authorization attempt.
It supplies configured clients, never a WebView-selected executable or endpoint.
"""

import json
from typing import TextIO

from .device_authorization import DeviceAuthorizationClient
from .registration_client import RegistrationClient


class EnrollmentWorker:
    def __init__(self, authorization: DeviceAuthorizationClient,
                 registration: RegistrationClient) -> None:
        self._authorization, self._registration = authorization, registration

    def handle(self, method: str, *, name: str | None = None) -> dict[str, object]:
        if method != "register" and name is not None:
            raise ValueError("Unexpected enrollment argument")
        if method == "start":
            self._authorization.start()
        elif method == "poll":
            self._authorization.poll()
        elif method == "cancel":
            self._authorization.cancel()
        elif method == "retry_save":
            self._authorization.retry_save()
        elif method == "register":
            if name is None:
                raise ValueError("Registration requires a name")
            saved = self._registration.current()
            progress = self._authorization.progress()
            if saved is None and progress.phase != "grant_saved":
                raise ValueError("Complete enrollment authorization before registration")
            self._registration.register(
                attempt_id=saved.attempt_id if saved is not None else progress.attempt_id,
                name=name,
            )
        elif method != "progress":
            raise ValueError("Unknown enrollment command")
        authorization = self._authorization.progress().model_dump(exclude={"credential_reference"})
        registration = self._registration.current()
        return {"schema_version": 1, "authorization": authorization,
                "registration": registration.model_dump() if registration is not None else None,
                "connection_state": "not_checked"}

    def close(self) -> None:
        self._authorization.cancel()
        self._registration.close()


def serve_enrollment(worker: EnrollmentWorker, reader: TextIO, writer: TextIO) -> None:
    """Bounded line protocol; serial calls retain state and never print secrets.

    EOF terminates this worker only, not the shared computer agent. Errors are
    intentionally generic: host diagnostics must not echo provider bodies.
    """
    try:
        while line := reader.readline(8193):
            if len(line) > 8192 or not line.endswith("\n"):
                raise ValueError("Invalid enrollment frame")
            try:
                request = json.loads(line)
                if (not isinstance(request, dict) or not set(request) <= {"method", "name"}
                        or not isinstance(request.get("method"), str)
                        or ("name" in request and not isinstance(request["name"], str))):
                    raise ValueError("Invalid enrollment command")
                response: dict[str, object] = {"ok": True, "result": worker.handle(**request)}
            except Exception:
                response = {"ok": False, "error": "Enrollment command was not confirmed"}
            writer.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
            writer.flush()
    finally:
        worker.close()
