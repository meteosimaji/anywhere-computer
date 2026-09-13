"""Isolated enrollment HTTP boundary; no personal MCP grants or PC execution."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .devices import device_name
from .http_mcp import HTTPMCP, HTTPResult
from .mcp_server import MCPSession
from .relay_enrollment import EnrollmentRejected, RelayEnrollment


class _Registration(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    enrollment_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return device_name(value)


def enrollment_http(service: RelayEnrollment) -> HTTPMCP:
    """Reuse bounded loopback framing, with separate enrollment authentication.

    Run the service and its registry on the owning event-loop thread. This is
    a loopback integration building block, not a public deployment command.
    No MCP grant is accepted, even when the bearer can register a device.
    """

    async def reject_mcp(token: str) -> None:
        return None

    def no_session(owner: str) -> MCPSession:
        raise RuntimeError("Enrollment does not provide MCP sessions")

    async def register(method: str, headers: dict[str, str], body: bytes,
                       query: str) -> HTTPResult:
        if method != "POST":
            return 405, None, {"Allow": "POST"}
        if query:
            return 400, None, {}
        if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return 415, None, {}
        if len(body) > 4096:
            return 413, None, {}
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or any(c.isspace() for c in token):
            return 401, None, {"WWW-Authenticate": "Bearer"}
        try:
            request = _Registration.model_validate_json(body)
        except ValueError:
            return 400, None, {}
        try:
            device = service.register(token, enrollment_id=request.enrollment_id, name=request.name)
        except EnrollmentRejected:
            return 401, None, {"WWW-Authenticate": "Bearer"}
        except ValueError:
            return 409, None, {}
        return 200, {"device_id": device.device_id, "enrollment_id": device.enrollment_id,
                     "name": device.name, "state": device.state}, {}

    return HTTPMCP(reject_mcp, no_session, public_routes={"/enrollment/devices": register})
