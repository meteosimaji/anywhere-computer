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


def text_field(text: str, expected: str) -> str | None:
    """Match a value only inside the provider's AXTextArea group, not a label."""
    section = re.search(r"^AXTextArea \([^\n]*\):\n(.*?)(?=^AX|\Z)", text, re.MULTILINE | re.DOTALL)
    if section is None:
        return None
    matches = [
        match[1]
        for line in section[1].splitlines()
        if "[not actionable]" not in line
        and "[value settable]" in line
        and f'value: "{expected}"' in line
        and (match := re.match(r"\s+(elem_\d+) - ", line))
    ]
    return matches[0] if len(matches) == 1 else None


async def verify(
    executable: Path,
    receipt: Path,
    *,
    typed: bool = False,
    window_id: int | None = None,
    app: str = "com.apple.calculator",
    expected_text: str | None = None,
) -> None:
    if typed and window_id is None:
        raise ValueError("Typed acceptance requires an explicitly selected --window-id")
    if expected_text is not None and not typed:
        raise ValueError("Text acceptance requires --typed and --window-id")
    report = {
        "completed": False,
        "route": "authenticated HTTP -> direct MCP -> Peekaboo",
        "model_inference_requested": False,
        "calls": [],
        "observed_results": [],
        "typed_gui": typed,
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
                    "gui_observe",
                    "gui_click",
                    "gui_type",
                    "gui_key",
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
                                assert (
                                    {"see", "type", "press"}
                                    if typed
                                    else {"see", "type", "hotkey", "app"}
                                ) <= names

                                observation_id = None

                                async def gui(name, arguments):
                                    nonlocal observation_id
                                    if typed and name == "see":
                                        result = await call(
                                            "gui_observe",
                                            {
                                                **sid,
                                                "app": arguments["app_target"],
                                                "window_id": window_id,
                                            },
                                        )
                                        observation_id = result["data"].get("observation_id")
                                    elif typed and name in {"type", "hotkey"}:
                                        assert observation_id is not None
                                        options = (
                                            arguments
                                            if name == "type"
                                            else {
                                                "keys": arguments["keys"].split(","),
                                            }
                                        )
                                        result = await call(
                                            "gui_type" if name == "type" else "gui_key",
                                            {**sid, **options, "observation_id": observation_id},
                                        )
                                        observation_id = None
                                    else:
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

                                if expected_text is not None:
                                    result = await gui("see", {"app_target": app})
                                    for replacement in (
                                        "Anywhere GUI 日本語 40 ✅",
                                        "Anywhere GUI 日本語 42 ✅",
                                    ):
                                        text = "\n".join(
                                            x.get("text", "")
                                            for x in result["data"]["content"]
                                            if x.get("type") == "text"
                                        )
                                        candidate = text_field(text, expected_text)
                                        assert candidate is not None, (
                                            "Expected one synthetic input field; no input sent"
                                        )
                                        changed = await gui(
                                            "type",
                                            {
                                                "text": replacement,
                                                "clear": True,
                                                "element_id": candidate,
                                            },
                                        )
                                        assert changed["data"]["postcondition_verified"] is False
                                        # Only observe while settling; never replay a mutation.
                                        deadline = time.monotonic() + 3
                                        while True:
                                            result = await gui("see", {"app_target": app})
                                            text = "\n".join(
                                                x.get("text", "")
                                                for x in result["data"]["content"]
                                                if x.get("type") == "text"
                                            )
                                            if text_field(text, replacement) is not None:
                                                break
                                            assert time.monotonic() < deadline, "Readback mismatch"
                                            await asyncio.sleep(0.1)
                                        report["observed_results"].append(replacement)
                                        recovered = await call(
                                            "operations_get",
                                            {"operation_id": changed["operation_id"]},
                                        )
                                        assert recovered["data"]["data"] == changed["data"]
                                        expected_text = replacement
                                else:
                                    await gui("see", {"app_target": app})
                                    if not typed:
                                        await gui("app", {"action": "focus", "name": app})
                                    await gui("hotkey", {"keys": "escape"})
                                    for expression, expected in [
                                        ("12+30", 42),
                                        ("+8", 50),
                                        ("+1", 51),
                                        ("+1", 52),
                                        ("+1", 53),
                                        ("+1", 54),
                                    ]:
                                        if typed and observation_id is None:
                                            await gui("see", {"app_target": app})
                                        await gui(
                                            "type", {"text": expression, "press_return": not typed}
                                        )
                                        if typed:
                                            await gui("see", {"app_target": app})
                                            await gui("hotkey", {"keys": "return"})
                                        deadline = time.monotonic() + 3
                                        observations = 0
                                        while True:
                                            result = await gui("see", {"app_target": app})
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
                                        report.setdefault("observation_counts", []).append(
                                            observations
                                        )
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
    parser.add_argument("--typed", action="store_true")
    parser.add_argument("--window-id", type=int)
    parser.add_argument("--app", default="com.apple.calculator")
    parser.add_argument(
        "--expected-text", help="Only edit a test field whose value matches exactly"
    )
    args = parser.parse_args()
    asyncio.run(
        verify(
            args.executable,
            args.receipt,
            typed=args.typed,
            window_id=args.window_id,
            app=args.app,
            expected_text=args.expected_text,
        )
    )
