"""Bounded metadata diagnostics; public HTTPS is explicit, credentials are never sent."""

import asyncio
import json
import shutil
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import JsonValue

from .http_service import load_http_config
from .watch_status import read_watch_observation


async def _metadata(port: int, *, resource: str | None = None) -> dict[str, JsonValue]:
    host, authority = "127.0.0.1", f"127.0.0.1:{port}"
    context = None
    if resource is not None:
        parsed = urlsplit(resource)
        host, authority, port = parsed.hostname or "", parsed.netloc, parsed.port or 443
        context = ssl.create_default_context()
    reader, writer = await asyncio.open_connection(
        host, port, limit=8192, ssl=context, server_hostname=host if context else None
    )
    try:
        writer.write(
            f"GET /.well-known/oauth-protected-resource HTTP/1.1\r\n"
            f"Host: {authority}\r\nAccept: application/json\r\n"
            "Connection: close\r\n\r\n".encode("ascii")
        )
        await writer.drain()
        header = await reader.readuntil(b"\r\n\r\n")
        if len(header) > 8192:
            raise ValueError("Metadata headers exceed limit")
        lines = header.decode("ascii").split("\r\n")
        status = lines[0].split(" ", 2)
        if len(status) < 2 or status[0] != "HTTP/1.1" or status[1] != "200":
            raise ValueError("Metadata endpoint did not return HTTP 200")
        headers: dict[str, str] = {}
        for line in lines[1:-2]:
            name, separator, value = line.partition(":")
            if not separator or name.lower() in headers:
                raise ValueError("Metadata headers are invalid")
            headers[name.lower()] = value.strip()
        length = headers.get("content-length", "")
        if not length.isascii() or not length.isdecimal() or len(length) > 5:
            raise ValueError("Metadata requires a bounded Content-Length")
        size = int(length)
        if not 1 <= size <= 16384 or any(
            name in headers for name in ("transfer-encoding", "content-encoding")
        ):
            raise ValueError("Metadata body is not supported")
        if headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise ValueError("Metadata is not JSON")
        data = json.loads(await reader.readexactly(size))
        if not isinstance(data, dict) or not isinstance(data.get("resource"), str):
            raise ValueError("Metadata resource is missing")
        return {"resource": data["resource"]}
    finally:
        writer.close()


async def diagnose_http(directory: Path) -> dict[str, JsonValue]:
    def report(state: str, action: str, **details: JsonValue) -> dict[str, JsonValue]:
        return {
            "state": state,
            "action": action,
            "changed": False,
            "public_reachability": "unverified",
            "authenticated": False,
            "supervisor_history": read_watch_observation(directory / "http-watch-status.json"),
            **details,
        }

    try:
        config = load_http_config(directory)
    except (OSError, ValueError):
        return report(
            "configuration_unavailable", "Inspect http-show or configure the HTTP service."
        )
    try:
        metadata = await asyncio.wait_for(_metadata(config.port), timeout=3)
    except (OSError, TimeoutError):
        return report(
            "unreachable",
            "Check the http-serve process and configured loopback port; "
            "this probe did not start or stop a service.",
            port=config.port,
        )
    except (ValueError, RecursionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return report(
            "unexpected_response",
            "The port responded without valid service metadata. "
            "Inspect the process using this port before starting another server.",
            port=config.port,
        )
    if metadata["resource"] != config.resource:
        return report(
            "resource_mismatch",
            "The port advertises a different resource. "
            "Inspect the running service and configuration.",
            port=config.port,
        )
    return report(
        "metadata_reachable",
        "Loopback metadata matches. Check client authorization and "
        "the public HTTPS route separately.",
        port=config.port,
        resource=config.resource,
    )


async def diagnose_remote(
    directory: Path, *, probe_public: bool = False, connector: str | None = None,
) -> dict[str, JsonValue]:
    """Inspect configuration and metadata without reading credentials or repairing state."""
    if connector is not None and (
        not Path(connector).is_absolute()
        or any(ord(char) < 32 or ord(char) == 127 for char in connector)
    ):
        raise ValueError("Diagnostic connector must be an absolute path without control characters")
    local = await diagnose_http(directory)
    public: dict[str, JsonValue] = {"state": "not_requested"}
    result: dict[str, JsonValue] = {
        "loopback": local,
        "supervisor_history": read_watch_observation(directory / "remote-watch-status.json"),
        "public": public,
        "connector": {
            "executable_available": shutil.which(connector or "cloudflared") is not None,
            "selection": "explicit_path" if connector is not None else "PATH",
            "version_state": "unverified",
            "process_state": "unverified",
        },
        "changed": False,
        "authenticated": False,
        "state": "attention_required",
    }
    if local["state"] == "configuration_unavailable":
        result["state"] = "configuration_unavailable"
        return result
    if probe_public:
        try:
            config = load_http_config(directory)
        except (OSError, ValueError):
            result["state"] = "configuration_unavailable"
            return result
        try:
            metadata = await asyncio.wait_for(_metadata(443, resource=config.resource), 5)
            public["state"] = (
                "metadata_reachable" if metadata["resource"] == config.resource
                else "resource_mismatch"
            )
        except ssl.SSLCertVerificationError:
            public["state"] = "certificate_verification_failed"
        except (OSError, TimeoutError):
            public["state"] = "unreachable"
        except (ValueError, RecursionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            public["state"] = "unexpected_response"
    if local["state"] == "metadata_reachable":
        if not probe_public:
            result["state"] = "local_metadata_reachable"
        elif public["state"] == "metadata_reachable":
            result["state"] = "local_and_public_metadata_reachable"
    if local["state"] != "metadata_reachable":
        result["action"] = local["action"]
    elif probe_public and public["state"] != "metadata_reachable":
        result["action"] = (
            "Loopback responds. Inspect the connector, configured HTTPS route, "
            "DNS and certificate; no repair was attempted."
        )
    else:
        result["action"] = (
            "Metadata is not proof of client authorization or connector ownership. "
            "Complete an authenticated MCP request to verify access."
        )
    return result
