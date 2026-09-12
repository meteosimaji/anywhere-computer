"""Opt-in live Calculator acceptance through authenticated HTTP and direct MCP.

Uses an already installed Peekaboo executable, its existing OS permissions, and
synthetic in-memory HTTP credentials. Never calls Peekaboo's model/agent tools.
This changes Calculator's current expression and brings its window forward.
"""

import argparse
import asyncio
import json
import re
import tempfile
import time
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.engine import Engine
from anywhere_computer.http_mcp import HTTPMCP


async def verify(executable: Path, receipt: Path) -> None:
    report = {
        "completed": False,
        "route": "authenticated HTTP -> direct MCP -> Peekaboo",
        "model_inference_requested": False,
        "calls": [],
        "observed_results": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="anywhere-gui-acceptance-") as directory:
            root = Path(directory)
            engine = Engine(root / "engine")
            scopes = frozenset(
                {
                    "computer_status",
                    "operations_get",
                    "mcp_session_open",
                    "mcp_tools",
                    "mcp_call",
                    "mcp_session_status",
                    "mcp_session_close",
                }
            )
            authority = AuthorizationStore(
                root / "auth", resource="https://fixture.test/mcp", known_tools=scopes
            )
            authority.register_client("chat", frozenset({"https://fixture.test/callback"}))
            authority.enroll_device("owner", "fixture", scopes)
            code = authority.approve(
                owner="owner",
                device="fixture",
                client="chat",
                redirect="https://fixture.test/callback",
                resource=authority.resource,
                tools=scopes,
                challenge=pkce_s256("v" * 43),
            )
            token = authority.exchange_code(
                code=code,
                verifier="v" * 43,
                client="chat",
                redirect="https://fixture.test/callback",
                resource=authority.resource,
            ).value
            backend = AuthorizedDeviceMCP(authority, engine, owner="owner", device="fixture")
            adapter = HTTPMCP(backend.authenticate, backend.session)
            port = await adapter.start()
            try:
                async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}) as http:
                    async with streamable_http_client(
                        f"http://127.0.0.1:{port}/mcp",
                        http_client=http,
                    ) as (reader, writer, _):
                        async with ClientSession(reader, writer) as client:
                            await client.initialize()

                            async def call(name, arguments):
                                start = time.perf_counter()
                                result = await client.call_tool(name, arguments)
                                row = result.structuredContent
                                assert row is not None
                                report["calls"].append(
                                    {
                                        "tool": name,
                                        "state": row["state"],
                                        "duration_ms": round(
                                            (time.perf_counter() - start) * 1000, 2
                                        ),
                                        "operation_id": row["operation_id"],
                                    }
                                )
                                assert not result.isError, name
                                assert row["state"] == "completed", name
                                return row

                            await call("computer_status", {})
                            opened = await call(
                                "mcp_session_open",
                                {
                                    "command": [str(executable), "mcp", "serve", "--no-remote"],
                                    "cwd": directory,
                                },
                            )
                            sid = {"session_id": opened["data"]["session_id"]}
                            try:
                                catalog = await call("mcp_tools", sid)
                                names = {t["name"] for t in catalog["data"]["tools"]}
                                assert {"see", "type", "hotkey", "app"} <= names

                                async def gui(name, arguments):
                                    result = await call(
                                        "mcp_call",
                                        {
                                            **sid,
                                            "name": name,
                                            "arguments": arguments,
                                        },
                                    )
                                    assert not result["data"]["is_error"], name
                                    return result

                                await gui("see", {"app_target": "com.apple.calculator"})
                                await gui(
                                    "app", {"action": "focus", "name": "com.apple.calculator"}
                                )
                                await gui("hotkey", {"keys": "escape"})
                                for expression, expected in [
                                    ("12+30", 42),
                                    ("+8", 50),
                                    ("+1", 51),
                                    ("+1", 52),
                                    ("+1", 53),
                                    ("+1", 54),
                                ]:
                                    await gui("type", {"text": expression, "press_return": True})
                                    deadline = time.monotonic() + 3
                                    observations = 0
                                    while True:
                                        result = await gui(
                                            "see", {"app_target": "com.apple.calculator"}
                                        )
                                        observations += 1
                                        text = "\n".join(
                                            x.get("text", "")
                                            for x in result["data"]["content"]
                                            if x.get("type") == "text"
                                        )
                                        # Observe UI settlement; never replay the input action.
                                        matched = re.search(
                                            r'elem_\d+ - "' + str(expected) + r'" - at', text
                                        )
                                        if matched:
                                            break
                                        assert time.monotonic() < deadline, (
                                            "Calculator did not settle",
                                            expected,
                                        )
                                        await asyncio.sleep(0.1)
                                    report.setdefault("observation_counts", []).append(observations)
                                    report["observed_results"].append(expected)
                                    recovered = await call(
                                        "operations_get",
                                        {
                                            "operation_id": result["operation_id"],
                                        },
                                    )
                                    assert recovered["data"]["data"] == result["data"]
                                assert (await call("mcp_session_status", sid))["data"][
                                    "state"
                                ] == "open"
                            finally:
                                closed = await call("mcp_session_close", sid)
                                report["cleanup_confirmed"] = closed["data"]["cleanup_confirmed"]
                            final = await call("computer_status", {})
                            assert final["data"]["active_resources"]["direct_mcp_sessions"] == 0
                            report["completed"] = True
            finally:
                await adapter.close()
                authority.close()
                await engine.close()
    finally:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "completed": report["completed"],
                    "observed_results": report["observed_results"],
                    "receipt": str(receipt),
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.executable, args.receipt))
