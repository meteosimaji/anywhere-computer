"""Bounded loopback metadata probe; no credentials, repairs, or public network calls."""

import asyncio
import json
from pathlib import Path

from pydantic import JsonValue

from .http_service import load_http_config


async def _metadata(port: int) -> dict[str, JsonValue]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=8192)
    try:
        writer.write(
            f"GET /.well-known/oauth-protected-resource HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\nAccept: application/json\r\n"
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
