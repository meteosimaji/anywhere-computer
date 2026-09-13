"""Fixed-command enrollment worker for a trusted native host.

The native host owns this worker for the duration of an authorization attempt.
It supplies configured clients, never a WebView-selected executable or endpoint.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, model_validator

from .authorization import validate_authorization_url
from .device_authorization import DeviceAuthorizationClient, EnrollmentProvider
from .enrollment_credentials import EnrollmentCredentials
from .registration_client import RegistrationClient


class NativeEnrollmentConfig(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    provider: EnrollmentProvider
    registration_endpoint: str

    @model_validator(mode="after")
    def enrollment_only(self) -> "NativeEnrollmentConfig":
        validate_authorization_url(self.registration_endpoint)
        if urlsplit(self.registration_endpoint).query:
            raise ValueError("Registration endpoint must not contain a query")
        if self.provider.scope != "device:enroll":
            raise ValueError("Native enrollment requires the registration scope")
        return self


class EnrollmentWorker:
    def __init__(self, authorization: DeviceAuthorizationClient,
                 registration: RegistrationClient) -> None:
        self._authorization, self._registration = authorization, registration

    def handle(self, method: str, *, name: str | None = None) -> dict[str, object]:
        if method != "register" and name is not None:
            raise ValueError("Unexpected enrollment argument")
        if method in {"progress", "register"}:
            self._authorization.restore_saved()
        if method == "start":
            self._authorization.start()
        elif method == "restart":
            if self._registration.current() is not None:
                raise ValueError("Recover the saved registration before reauthorizing")
            self._authorization = self._authorization.new_attempt()
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        if not args.state_dir.is_absolute() or not args.config.is_absolute():
            raise ValueError("Native paths must be absolute")
        if args.config.is_symlink():
            raise ValueError("Native configuration must not be a symbolic link")
        with args.config.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("Native configuration exceeds limit")
        config = NativeEnrollmentConfig.model_validate_json(raw)
        credentials = EnrollmentCredentials(
            args.state_dir, issuer=config.provider.issuer, client=config.provider.client_id,
            profile="native-manager",
        )
        authorization = DeviceAuthorizationClient(config.provider, credentials)
        registration = RegistrationClient(args.state_dir, credentials,
                                          endpoint=config.registration_endpoint)
        serve_enrollment(EnrollmentWorker(authorization, registration), sys.stdin, sys.stdout)
    except Exception:
        print(json.dumps({"ok": False, "error": "Native enrollment worker could not continue"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
