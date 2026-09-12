"""The same loopback protocol runs on Windows, Linux and macOS."""

import asyncio
import hmac
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import cast

import psutil
from pydantic import Field, JsonValue

from .credentials import local_credential
from .engine import Engine
from .engine_selection import engine_directory
from .locking import ProcessLock
from .models import Contract, Reply, Request
from .runtime_identity import ENGINE_API_VERSION, runtime_identity
from .runtime_launch import python_module_command
from .state import prepare_directory

WIRE_LIMIT = 8 * 1024 * 1024
# Includes unauthenticated readers. Admission happens synchronously before a task
# can retain a full legacy credential-bearing frame (8 MiB per connection).
MAX_CONNECTIONS = 8


class GrantedRequest(Contract):
    """Trusted local gateway envelope; never registered as a public MCP tool."""

    identity: str = Field(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
    tools: list[str] = Field(max_length=64)
    request: Request


async def exchange_remote(
    directory: Path, identity: str, allowed: frozenset[str], request: Request,
    *, credential: str | None = None,
) -> Reply:
    envelope = GrantedRequest(identity=identity, tools=sorted(allowed), request=request)
    return await exchange(
        directory, "__remote", cast(dict[str, JsonValue], envelope.model_dump(mode="json")),
        operation_id=request.operation_id, credential=credential,
    )


def load_endpoint(directory: Path) -> dict[str, JsonValue]:
    payload = json.loads((directory / "agent.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Invalid local agent endpoint")
    return cast(dict[str, JsonValue], payload)


async def exchange(
    directory: Path,
    tool: str,
    arguments: dict[str, JsonValue] | None = None,
    *,
    operation_id: str | None = None,
    timeout: float = 65,
    credential: str | None = None,
) -> Reply:
    endpoint = load_endpoint(directory)
    port = endpoint.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
        raise ValueError("Invalid local agent endpoint")
    request = Request(
        operation_id=operation_id or uuid.uuid4().hex, tool=tool, arguments=arguments or {}
    )
    secret = credential if credential is not None else local_credential(directory)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port, limit=WIRE_LIMIT),
        timeout=min(timeout, 5),
    )
    try:
        message = json.dumps({"credential": secret, "request": request.model_dump()}) + "\n"
        if len(message.encode()) > WIRE_LIMIT:
            raise ValueError("Request exceeds local transport size limit")
        writer.write(message.encode())
        await writer.drain()
        payload = await asyncio.wait_for(reader.readline(), timeout)
        if not payload:
            raise ConnectionError("Agent closed the connection before returning an outcome")
        reply = Reply.model_validate_json(payload)
        if reply.operation_id != request.operation_id:
            raise ConnectionError("Agent returned a different operation ID")
        return reply
    finally:
        writer.close()
        await writer.wait_closed()


async def serve(
    directory: Path, *, credential: str | None = None, shutdown: asyncio.Event | None = None
) -> None:
    prepare_directory(directory)
    secret = credential if credential is not None else local_credential(directory)
    stop = shutdown or asyncio.Event()
    with ProcessLock(directory / "agent.lock", timeout=0):
        engine = Engine(engine_directory(directory), file_locks=directory / "file-locks")
        connections: set[asyncio.Task[None]] = set()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            current = asyncio.current_task()
            try:
                packet = json.loads(await asyncio.wait_for(reader.readline(), 10))
                if not isinstance(packet, dict):
                    return
                supplied = packet.get("credential")
                if not isinstance(supplied, str) or not hmac.compare_digest(secret, supplied):
                    return
                request = Request.model_validate(packet.get("request"))
                if stop.is_set():
                    # A connection may have been accepted before __stop but finish
                    # reading afterwards. Do not start new work while draining it.
                    reply = Reply(
                        operation_id=request.operation_id,
                        state="failed",
                        error="Agent is stopping; retry the same operation ID after reconnecting",
                    )
                elif request.tool == "__status":
                    reply = Reply(
                        operation_id=request.operation_id, state="completed", data=engine.status()
                    )
                elif request.tool == "__catalog":
                    reply = Reply(
                        operation_id=request.operation_id,
                        state="completed",
                        data={"tools": engine.catalog()},
                    )
                elif request.tool == "__remote":
                    from .remote_bridge import RemoteAgent

                    granted = GrantedRequest.model_validate(request.arguments)
                    if granted.request.operation_id != request.operation_id:
                        raise ValueError("Forwarded operation ID differs from envelope")
                    bridge = RemoteAgent(
                        engine, {granted.identity: frozenset(granted.tools)}, transport="http",
                    )
                    reply = Reply.model_validate_json(await bridge.dispatch(
                        granted.identity, granted.request.model_dump_json().encode(),
                    ))
                elif request.tool == "__stop":
                    if engine.status()["update_blocked"]:
                        reply = Reply(
                            operation_id=request.operation_id,
                            state="failed",
                            error="Active work exists; stop sessions before stopping agent",
                            data={"active_resources": engine.status()["active_resources"]},
                        )
                    else:
                        reply = Reply(
                            operation_id=request.operation_id,
                            state="completed",
                            data={"state": "stopping"},
                        )
                        stop.set()
                else:
                    reply = await engine.execute(request)
                response = reply.model_dump_json().encode() + b"\n"
                if len(response) > WIRE_LIMIT:
                    response = (
                        Reply(
                            operation_id=request.operation_id,
                            state="unknown",
                            error="Result exceeded the transport limit; query operation "
                            "status or request a smaller page",
                        )
                        .model_dump_json()
                        .encode()
                    )
                    response += b"\n"
                writer.write(response)
                await asyncio.wait_for(writer.drain(), 5)
            except (ValueError, OSError, TimeoutError):
                # Do not log packet contents: they may contain credentials or private files.
                pass
            finally:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                except (OSError, TimeoutError):
                    writer.transport.abort()
                if current:
                    connections.discard(current)

        def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if len(connections) >= MAX_CONNECTIONS or stop.is_set():
                writer.transport.abort()
                return
            connections.add(asyncio.create_task(handle(reader, writer)))

        server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=WIRE_LIMIT)
        port = server.sockets[0].getsockname()[1]
        metadata = {
            "port": port,
            "pid": os.getpid(),
            "process_started": psutil.Process().create_time(),
            "instance_id": engine.instance_id,
            "version": engine.status()["version"],
        }
        pending = directory / "agent.pending.json"
        pending.write_text(json.dumps(metadata))
        pending.replace(directory / "agent.json")
        loop = asyncio.get_running_loop()
        if os.name != "nt":
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
        try:
            async with server:
                await stop.wait()
        finally:
            server.close()
            await server.wait_closed()
            if connections:
                await asyncio.gather(*list(connections), return_exceptions=True)
            await engine.close()
            (directory / "agent.json").unlink(missing_ok=True)


def ensure_agent(directory: Path, *, replace_idle: bool = False) -> dict[str, JsonValue]:
    prepare_directory(directory)
    expected_runtime = runtime_identity()
    with ProcessLock(directory / "startup.lock", timeout=15):
        engine_directory(directory)  # Never start against a half-switched or missing store.
        credential = local_credential(directory, create=True)
        try:
            reply = asyncio.run(exchange(directory, "__status", timeout=2, credential=credential))
            if reply.state == "completed":
                if reply.data.get("runtime_id") == expected_runtime:
                    return reply.data
                if not replace_idle:
                    api = reply.data.get("engine_api_version")
                    if type(api) is int and api == ENGINE_API_VERSION:
                        return reply.data
                    raise RuntimeError(
                        "Running engine API is incompatible or undeclared. Update the engine "
                        "through its launcher; this connector did not stop or replace it."
                    )
                if (reply.data.get("update_blocked") or reply.data.get("active_sessions")
                        or reply.data.get("active_operations")):
                    raise RuntimeError(
                        "A different agent build has active work. Finish it with the previous "
                        "installation before upgrading; no process was stopped."
                    )
                stopped = asyncio.run(exchange(directory, "__stop", credential=credential))
                if stopped.state != "completed":
                    raise RuntimeError("Agent became busy during upgrade; no restart performed")
                deadline = time.monotonic() + 5
                while (directory / "agent.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if (directory / "agent.json").exists():
                    raise RuntimeError("Previous agent has not finished shutdown; retry later")
        except (OSError, ValueError, TimeoutError):
            pass
        try:
            metadata = load_endpoint(directory)
            pid, created = metadata.get("pid"), metadata.get("process_started")
            if isinstance(pid, int) and psutil.pid_exists(pid):
                if psutil.Process(pid).create_time() == created:
                    raise RuntimeError(
                        "Agent process exists but is not responding. "
                        "Run anywhere doctor; active work has been left untouched."
                    )
        except (OSError, ValueError, psutil.NoSuchProcess):
            pass
        command = python_module_command("anywhere_computer", "serve", "--state-dir", str(directory))
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=(0x00000008 | 0x00000200) if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                reply = asyncio.run(
                    exchange(directory, "__status", timeout=1, credential=credential)
                )
                if reply.state == "completed":
                    return reply.data
            except (OSError, ValueError, TimeoutError):
                pass
            time.sleep(0.1)
        raise RuntimeError("Agent did not become ready. Run anywhere serve to see startup errors.")
