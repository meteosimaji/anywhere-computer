"""Disposable browser host for the real workspace resource and MCP engine.

Development only. Serves no user files or credentials. Stop with Ctrl-C to remove
its temporary files. The simulated host is not evidence of ChatGPT/Codex acceptance.
"""

import asyncio
import base64
import json
import tempfile
from pathlib import Path

from pydantic import JsonValue

from anywhere_computer.engine import Engine
from anywhere_computer.mcp_server import MCPSession
from anywhere_computer.models import Reply, Request
from anywhere_computer.workspace_ui import UI_EXTENSION, UI_MIME, workspace_resource

FIXTURE_TOOLS = frozenset(
    {
        "workspace_open",
        "files_info",
        "files_read",
        "files_write",
        "files_read_binary",
        "directories_list",
        "documents_read",
        "settings_get",
        "settings_update",
        "operations_get",
    }
)
HOST_HTML = """<!doctype html><meta charset="utf-8"><title>Workspace browser verification</title>
<style>body{margin:24px;background:#ededf0;font:14px system-ui}p{color:#45554a}
iframe{display:block;width:min(920px,100%);height:820px;border:1px solid #b6c4b8;
border-radius:16px;background:#fafaf7}</style>
<p>Disposable verification host · real MCP engine · temporary files only</p>
<iframe title="Anywhere Computer workspace" sandbox="allow-scripts" src="/workspace"></iframe>
<script>
const frame=document.querySelector('iframe');
const send=packet=>frame.contentWindow.postMessage({jsonrpc:'2.0',...packet},'*');
const call=async packet=>{
  const response=await fetch('/rpc',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(packet)});
  if(!response.ok) throw new Error('Fixture request rejected');
  return await response.json();
};
window.addEventListener('message',async event=>{
  if(event.source!==frame.contentWindow || event.data?.jsonrpc!=='2.0') return;
  const packet=event.data;
  try {
    if(packet.method==='ui/initialize') send({id:packet.id,result:{
      protocolVersion:'2026-01-26',hostCapabilities:{serverTools:{}},hostContext:{theme:'light'}}});
    else if(packet.method==='ui/notifications/initialized') {
      const opened=await call({jsonrpc:'2.0',id:1,method:'tools/call',
        params:{name:'workspace_open',arguments:{path:FIXTURE_PATH}}});
      send({method:'ui/notifications/tool-result',params:opened.result});
    } else if(packet.method==='tools/call') send(await call(packet));
    else if(packet.method==='ui/notifications/size-changed' &&
      Number.isFinite(packet.params?.height))
      frame.style.height=Math.max(400,Math.min(1400,packet.params.height))+'px';
  } catch(error) {if(packet.id) send({id:packet.id,error:{code:-32603,message:error.message}});}
});
</script>"""


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="anywhere-browser-fixture-") as raw:
        root = Path(raw)
        fixture = root / "files"
        fixture.mkdir()
        (fixture / "notes.txt").write_bytes(b"First line\r\nSecond line\r\n")
        (fixture / "long.txt").write_text("".join(f"Line {i}\n" for i in range(1, 251)))
        (fixture / "literal.html").write_text("<script>window.fixtureInjected=true</script>")
        (fixture / "pixel.png").write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jfZkAAAAASUVORK5CYII="
            )
        )
        engine = Engine(root / "engine")

        async def catalog() -> list[JsonValue]:
            return engine.catalog(FIXTURE_TOOLS)

        async def execute(request: Request) -> Reply:
            if request.tool not in FIXTURE_TOOLS:
                raise ValueError("Tool is outside this fixture")
            path = request.arguments.get("path")
            if path is not None and (
                not isinstance(path, str)
                or not Path(path).is_absolute()
                or not Path(path).resolve().is_relative_to(fixture.resolve())
            ):
                raise ValueError("Path is outside this disposable fixture")
            return await engine.execute(request)

        session = MCPSession(catalog, execute)
        await session.handle(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "browser-fixture", "version": "1"},
                    "capabilities": {"extensions": {UI_EXTENSION: {"mimeTypes": [UI_MIME]}}},
                },
            }
        )
        await session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        host = HOST_HTML.replace("FIXTURE_PATH", json.dumps(str(fixture)))
        origin = ""

        async def connection(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            status, mime, data = "400 Bad Request", "text/plain", b"Rejected"
            try:
                async with asyncio.timeout(10):
                    header = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
                    lines = header.split("\r\n")
                    method, target, _ = lines[0].split(" ")
                    fields = {}
                    for line in lines[1:]:
                        if line:
                            key, value = line.split(":", 1)
                            if key.lower() in fields:
                                raise ValueError("Duplicate header")
                            fields[key.lower()] = value.strip()
                    if fields.get("host") != origin.removeprefix("http://"):
                        raise ValueError("Invalid host")
                    if method == "GET" and target in {"/", "/workspace"}:
                        text = host if target == "/" else workspace_resource()["text"]
                        data, mime, status = (
                            str(text).encode(),
                            "text/html; charset=utf-8",
                            "200 OK",
                        )
                    elif method == "POST" and target == "/rpc":
                        if (
                            fields.get("origin") != origin
                            or fields.get("content-type") != "application/json"
                        ):
                            raise ValueError("Invalid origin or content type")
                        size = int(fields.get("content-length", "0"))
                        if not 0 < size <= 2 * 1024 * 1024 or "transfer-encoding" in fields:
                            raise ValueError("Invalid body size")
                        packet = json.loads(await reader.readexactly(size))
                        if not isinstance(packet, dict) or packet.get("method") != "tools/call":
                            raise ValueError("Only fixture tool calls are allowed")
                        result = await session.handle(packet)
                        data = json.dumps(result, allow_nan=False).encode()
                        mime, status = "application/json", "200 OK"
            except (
                ValueError,
                OSError,
                TimeoutError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ):
                pass
            try:
                writer.write(
                    (
                        f"HTTP/1.1 {status}\r\nContent-Type: {mime}\r\n"
                        f"Content-Length: {len(data)}\r\nConnection: close\r\n"
                        "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
                        "Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; "
                        "style-src 'unsafe-inline'; frame-src 'self'; connect-src 'self'; "
                        "img-src blob:; base-uri 'none'; form-action 'none'\r\n\r\n"
                    ).encode()
                    + data
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            server = await asyncio.start_server(connection, "127.0.0.1", 0, limit=16384)
            origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            print(json.dumps({"url": origin, "fixture": str(fixture)}), flush=True)
            async with server:
                await server.serve_forever()
        finally:
            await engine.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
