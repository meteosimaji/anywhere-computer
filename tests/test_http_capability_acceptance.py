"""Chat-like HTTP workflows over real files/processes and synthetic Codex data.

No model generation, private conversation reads or production settings changes.
The Codex app-server peer is a subprocess protocol fixture; this is not a claim
that every installed third-party plugin or OS GUI action was tested.
"""

import asyncio
import base64
import hashlib
import os
import sys
import uuid

import httpx
import psutil
import pytest
from test_audio_capture import audio_helper as audio_helper
from test_http_service import initialize
from test_native_gui import helper_process as helper_process

from anywhere_computer.authorization import LOCAL_ONLY_TOOLS, AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.engine import Engine
from anywhere_computer.http_mcp import HTTPMCP


@pytest.mark.skipif(os.name == "nt", reason="POSIX app-server executable fixture")
async def test_every_remote_engine_tool_in_chat_like_http_workflows(
    tmp_path, monkeypatch, helper_process, audio_helper,
):
    skill = tmp_path / "skill/SKILL.md"
    skill.parent.mkdir()
    skill.write_text("Read reference.txt for the fixture.\n", encoding="utf-8")
    (skill.parent / "reference.txt").write_text("reference fixture", encoding="utf-8")
    executable = tmp_path / "codex-fixture"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        + f"SKILL={str(skill)!r}\n"
        + """
import json,sys
n=40
for line in sys.stdin:
 p=json.loads(line);m=p.get('method')
 if 'id' not in p:continue
 if m=='initialize':r={}
 elif m=='thread/start':
  assert p['params']['ephemeral'] is True
  r={'thread':{'id':'fixture','ephemeral':True}}
 elif m=='thread/list':r={'data':[{'id':'fixture','title':'Acceptance fixture'}]}
 elif m=='thread/turns/list':
  r={'threadId':'fixture','data':[{'items':[{'id':'u','type':'userMessage',
     'content':[{'type':'input_text','text':'fixture message'}]}]}]}
 elif m=='skills/list':
  r={'data':[{'errors':[],'skills':[{'path':SKILL,'name':'fixture','description':'fixture',
                        'enabled':True}]}]}
 elif m=='mcpServerStatus/list':
  r={'data':[{'name':'fixture','runtimeStatus':'connected','tools':[{
     'name':'increment','description':'Increment a counter',
     'inputSchema':{'type':'object','properties':{}}}]}]}
 elif m=='mcpServer/tool/call':
  n+=1;r={'content':[{'type':'text','text':str(n)}],'isError':False}
 elif m=='thread/unsubscribe':r={}
 else:raise RuntimeError('Unexpected method: '+str(m))
 print(json.dumps({'jsonrpc':'2.0','id':p['id'],'result':r}),flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("ANYWHERE_CODEX_EXECUTABLE", str(executable))
    monkeypatch.setenv("PATH", str(os.path.dirname(sys.executable)) + os.pathsep + os.defpath)
    engine = Engine(tmp_path / "engine")
    known = frozenset(engine.tools) - LOCAL_ONLY_TOOLS
    authority = AuthorizationStore(
        tmp_path / "auth", resource="https://fixture.test/mcp", known_tools=known
    )
    authority.register_client("chat", frozenset({"https://fixture.test/callback"}))
    authority.enroll_device("owner", "fixture", known)
    code = authority.approve(
        owner="owner",
        device="fixture",
        client="chat",
        redirect="https://fixture.test/callback",
        resource=authority.resource,
        tools=known,
        challenge=pkce_s256("v" * 43),
    )
    token = authority.exchange_code(
        code=code,
        verifier="v" * 43,
        client="chat",
        redirect="https://fixture.test/callback",
        resource=authority.resource,
    ).value
    backend = AuthorizedDeviceMCP(authority, engine, owner="owner", device="fixture", client="chat")
    adapter = HTTPMCP(backend.authenticate, backend.session)
    port = await adapter.start()
    covered = set()
    process = None
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=40) as http:
            headers = await initialize(http, token)

            async def discover():
                response = await http.post('/mcp', headers=headers, json={
                    'jsonrpc': '2.0', 'id': uuid.uuid4().hex, 'method': 'tools/list',
                })
                assert response.status_code == 200
                tools = response.json()['result']['tools']
                assert {tool['name'] for tool in tools} == known
                assert all(isinstance(tool['inputSchema'], dict) for tool in tools)
                return {tool['name']: tool for tool in tools}

            discovered = await discover()

            async def call(name, args=None, *, operation=None, expected="completed"):
                assert name in discovered, f'Tool was not discovered in this session: {name}'
                response = await http.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": uuid.uuid4().hex,
                        "method": "tools/call",
                        "params": {
                            "name": name,
                            "arguments": args or {},
                            "_meta": {
                                "io.github.meteosimaji.anywhere-computer/operation_id": operation
                                or uuid.uuid4().hex,
                            },
                        },
                    },
                )
                assert response.status_code == 200
                result = response.json()["result"]["structuredContent"]
                assert result["state"] == expected, (name, result)
                covered.add(name)
                return result["data"]

            status = await call("computer_status")
            assert status["active_sessions"] == 0
            browser = await call("browser_open")
            browser_ids = {"session_id": browser["session_id"], "tab_id": browser["tab_id"]}
            browser_url = f"http://127.0.0.1:{port}/browser-fixture"
            navigated = await call("browser_navigate", {**browser_ids, "url": browser_url})
            assert navigated["url"] == browser_url
            assert (await call("browser_observe", browser_ids))["tab_id"] == browser["tab_id"]
            assert (await call("browser_close", browser_ids))["state"] == "closed"
            audio = await call("audio_status")
            assert audio["state"] in {"available", "unavailable", "unsupported"}
            assert audio["capture_started"] is False
            assert audio["capture_tool_available"] is True
            recording = await call("audio_capture", {"source": "system", "seconds": 1,
                "output_directory": str(tmp_path / "audio-recording")})
            assert recording["state"] == "captured" and not recording["microphone_used"]
            assert len(audio_helper) == 1
            native = await call("gui_native_windows", {"app": "fixture"})
            target = {"session_id": native["session_id"], "app": "fixture", "window_id": 1}
            snapshot = await call("gui_native_observe", target)
            written = await call("gui_native_set_value", {
                **target, "observation_id": snapshot["observation_id"],
                "element_ref": "field", "value": "HTTP native fixture",
            })
            assert written["value_verified"] and not written["persistence_verified"]
            snapshot = await call("gui_native_observe", target)
            pressed = await call("gui_native_press", {
                **target, "observation_id": snapshot["observation_id"], "element_ref": "button",
            })
            assert pressed["action_accepted"] and not pressed["postcondition_verified"]
            await call("gui_native_close", {"session_id": native["session_id"]})
            await call("workspace_open", {"path": str(tmp_path)})
            await call("settings_get")
            await call("settings_update", {"key": "file_read_line_limit", "value": 123})
            await call("directories_create", {"path": str(tmp_path / "work")})
            path = str(tmp_path / "work/日本語.txt")
            text = "日本語 🚀\n40\n"
            write_id = uuid.uuid4().hex
            await call("files_write", {"path": path, "text": text}, operation=write_id)
            await call("files_write", {"path": path, "text": text}, operation=write_id)
            read = await call("files_read", {"path": path})
            assert read["text"] == text
            edited = await call(
                "files_edit",
                {
                    "path": path,
                    "old_text": "40",
                    "new_text": "42",
                    "expected_sha256": read["sha256"],
                },
            )
            await call(
                "files_restore",
                {
                    "path": path,
                    "backup_id": edited["backup_id"],
                    "expected_sha256": edited["sha256"],
                },
            )
            assert (await call("files_read", {"path": path}))["text"] == text
            await call("files_read_many", {"paths": [path]})
            await call("files_info", {"path": path})
            moved = str(tmp_path / "work/moved.txt")
            await call("files_move", {"source": path, "destination": moved})
            assert not os.path.exists(path) and os.path.isfile(moved)
            await call("directories_list", {"path": str(tmp_path / "work")})
            payload = b"\x00binary\xff" * 40
            encoded = base64.b64encode(payload).decode()
            binary = str(tmp_path / "binary.dat")
            await call("files_write_binary", {"path": binary, "data_base64": encoded})
            data = await call("files_read_binary", {"path": binary})
            assert base64.b64decode(data["data_base64"]) == payload
            download = {"transfer_id": uuid.uuid4().hex}
            await call("download_begin", {**download, "path": binary})
            await call("download_status", download)
            assert (
                base64.b64decode((await call("download_read", download))["data_base64"]) == payload
            )
            await call("download_close", download)
            upload = {"transfer_id": uuid.uuid4().hex}
            target = tmp_path / "uploaded.bin"
            await call(
                "upload_begin",
                {
                    **upload,
                    "path": str(target),
                    "total_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            )
            await call("upload_chunk", {**upload, "offset": 0, "data_base64": encoded})
            await call("upload_status", upload)
            await call("upload_commit", upload)
            assert target.read_bytes() == payload
            await call(
                "upload_resolve", {**upload, "action": "confirm_published"}, expected="failed"
            )  # Completed uploads cannot be resolved again.
            abandoned = {"transfer_id": uuid.uuid4().hex}
            await call(
                "upload_begin",
                {
                    **abandoned,
                    "path": str(tmp_path / "aborted"),
                    "total_bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                },
            )
            await call("upload_abort", abandoned)
            for format_ in ("docx", "xlsx"):
                document = str(tmp_path / ("document." + format_))
                content = {"text": "Document 日本語"} if format_ == "docx" else {"rows": [[40, 42]]}
                await call("documents_write", {"path": document, "format": format_, **content})
                result = await call("documents_read", {"path": document})
                assert "Document" in str(result) if format_ == "docx" else "42" in str(result)
                if format_ == "docx":
                    edited = await call("documents_edit_paragraph", {
                        "path": document, "paragraph": 1,
                        "expected_sha256": result["sha256"],
                        "expected_text": "Document 日本語", "new_text": "Document 更新済み",
                    })
                    assert edited["diff"] == {
                        "paragraph": 1, "before": "Document 日本語",
                        "after": "Document 更新済み",
                    }
                    assert (await call("documents_read", {"path": document}))["entries"][0][
                        "text"
                    ] == "Document 更新済み"
            search = await call(
                "search_start",
                {"path": str(tmp_path / "work"), "pattern": "日本語", "kind": "text"},
            )
            sid = {"search_id": search["search_id"]}
            async with asyncio.timeout(5):
                while True:
                    page = await call("search_results", sid)
                    if page.get("results"):
                        break
                    await asyncio.sleep(0.02)
            await call("search_list")
            await call("search_stop", sid)
            terminal = await call(
                "terminal_start", {"command": "/bin/cat", "shell": "/bin/sh", "cwd": str(tmp_path)}
            )
            tid = {"session_id": terminal["session_id"]}
            await call("terminal_input", {**tid, "text": "acceptance-42\n", "wait_ms": 200})
            output = await call("terminal_output", {**tid, "wait_ms": 200})
            assert "acceptance-42" in str(output)
            await call("terminal_list")
            await call("terminal_stop", tid)
            interactive = await call("terminal_start", {
                "command": "/bin/cat", "shell": "/bin/sh", "cwd": str(tmp_path),
                "interactive": True,
            })
            pty_id = {"session_id": interactive["session_id"]}
            resized = await call("terminal_resize", {**pty_id, "rows": 40, "columns": 100})
            assert resized["rows"] == 40 and resized["columns"] == 100
            await call("terminal_stop", pty_id)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time;time.sleep(60)"
            )
            created = psutil.Process(process.pid).create_time()
            await call("processes_list", {"after_pid": max(0, process.pid - 1), "limit": 1})
            await call("processes_stop", {"pid": process.pid, "created": created})
            await asyncio.wait_for(process.wait(), 5)
            await call("codex_threads_list")
            thread = await call("codex_thread_read", {"thread_id": "fixture"})
            assert thread["messages"][0]["text"] == "fixture message"
            skills = await call("codex_skills_list", {"cwd": str(tmp_path)})
            body = await call(
                "codex_skill_read",
                {"cwd": str(tmp_path), "skill_id": skills["skills"][0]["skill_id"]},
            )
            assert body["skill_directory"] == str(skill.parent.resolve())
            await call("files_read", {"path": str(skill.parent / "reference.txt")})
            common = await call("skills_list", {"roots": [str(skill.parent.parent)]})
            common_row = next(row for row in common["skills"]
                              if row["skill_directory"] == str(skill.parent.resolve()))
            common_resource = await call("skills_read", {
                "roots": [str(skill.parent.parent)], "skill_id": common_row["skill_id"],
                "expected_skill_sha256": common_row["skill_sha256"],
                "relative_path": "reference.txt",
            })
            assert common_resource["text"] == (skill.parent / "reference.txt").read_text(
                encoding="utf-8",
            )
            await call("settings_update", {
                "key": "skill_roots", "value": [str(skill.parent.parent)],
            })
            defaults = await call("settings_get")
            assert defaults["skill_roots"] == [str(skill.parent.parent.resolve())]
            saved_common = await call("skills_list")
            assert saved_common["skills"] == common["skills"]
            saved_resource = await call("skills_read", {
                "skill_id": common_row["skill_id"],
                "expected_skill_sha256": common_row["skill_sha256"],
                "relative_path": "reference.txt",
            })
            assert saved_resource["text"] == common_resource["text"]
            await call("settings_update", {"key": "skill_roots", "value": []})
            plugin = await call("codex_plugin_session_open", {"cwd": str(tmp_path)})
            psid = {"session_id": plugin["session_id"]}
            selection = {**psid, "cwd": str(tmp_path), "server": "fixture", "tool": "increment"}
            catalog = await call("codex_plugin_tools", selection)
            tool = catalog["servers"][0]["tools"][0]
            for value in (41, 42):
                result = await call(
                    "codex_plugin_call", {**selection, "catalog_sha256": tool["catalog_sha256"]}
                )
                assert result["content"][0]["text"] == str(value)
            assert (await call("codex_plugin_session_status", psid))["state"] == "open"
            assert (await call("codex_plugin_session_close", psid))["cleanup_confirmed"]
            mcp = tmp_path / "peer.py"
            mcp.write_text(
                "from mcp.server.fastmcp import FastMCP\n"
                "from mcp.types import CallToolResult, TextContent\n"
                "m=FastMCP('fixture')\nn=40\n"
                "@m.tool()\ndef increment()->int:\n global n\n n+=1\n return n\n"
                "@m.tool()\ndef see(app_target:str,window_id:int)->CallToolResult:\n"
                " return CallToolResult(content=[TextContent(type='text',"
                "text='Snapshot ID: fixture-1\\nApplication: fixture\\n  elem_1 - button')],"
                "_meta={'snapshot_id':'fixture-1',"
                "'target_receipt':{'window_id':window_id}})\n"
                "@m.tool()\ndef click(on:str,snapshot:str)->str:\n return 'clicked'\n"
                "@m.tool()\ndef app(action:str,name:str)->str:\n return 'focused'\n"
                "@m.tool()\ndef type(text:str,clear:bool,snapshot:str,"
                "on:str='')->str:\n return text\n"
                "@m.tool()\ndef press(keys:list[str],snapshot:str)->str:\n return ','.join(keys)\n"
                "m.run(transport='stdio')\n"
            )
            direct = await call(
                "mcp_session_open",
                {
                    "command": [sys.executable, "-I", str(mcp)],
                    "cwd": str(tmp_path),
                },
            )
            dsid = {"session_id": direct["session_id"]}
            await call("mcp_tools", dsid)
            for value in (41, 42):
                result = await call("mcp_call", {**dsid, "name": "increment"})
                assert result["content"][0]["text"] == str(value)
            for name, arguments in (
                ("gui_click", {"element_id": "elem_1"}),
                ("gui_type", {"text": "fixture", "press_return": False}),
                ("gui_key", {"keys": ["escape"]}),
            ):
                observed = await call("gui_observe", {**dsid, "app": "fixture", "window_id": 42})
                assert observed["action_ready"]
                action = await call(name, {**dsid, **arguments,
                    "observation_id": observed["observation_id"]})
                assert action["observation_consumed"] and not action["is_error"]
            await call("mcp_session_status", dsid)
            assert (await call("mcp_session_close", dsid))["cleanup_confirmed"]
            assert (await call("mcp_watch_list"))["watches"] == []
            assert (await call("operations_get", {"operation_id": write_id}))[
                "state"
            ] == "completed"
            await call("usage_stats")
            assert covered == known, {"uncovered": sorted(known - covered)}
            assert (await call("computer_status"))["active_sessions"] == 0

            # A new chat must bootstrap independently rather than inherit the old
            # MCP session or its discovery cache. Retain only the account grant
            # and an explicitly supplied receipt for the recovery scenario.
            previous_headers = dict(headers)
            assert (await http.delete('/mcp', headers=headers)).status_code == 200
            discovered.clear()
            headers = await initialize(http, token)
            assert headers['MCP-Session-Id'] != previous_headers['MCP-Session-Id']
            stale = await http.post('/mcp', headers=previous_headers, json={
                'jsonrpc': '2.0', 'id': 'stale', 'method': 'tools/list',
            })
            assert stale.status_code == 404
            discovered = await discover()
            assert (await call('computer_status'))['active_sessions'] == 0
            recovered = await call('operations_get', {'operation_id': write_id})
            assert recovered['state'] == 'completed'
            fresh_path = str(tmp_path / 'fresh-chat.txt')
            await call('files_write', {'path': fresh_path, 'text': '新規チャット 🚀'})
            assert (await call('files_read', {'path': fresh_path}))['text'] == '新規チャット 🚀'
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await adapter.close()
        authority.close()
        await engine.close()
