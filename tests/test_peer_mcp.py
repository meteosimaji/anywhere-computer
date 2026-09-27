"""Two independent local MCP processes exchange a saved peer message."""

import asyncio
import os
import subprocess
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from anywhere_computer import peer_cli
from anywhere_computer.peer_mailbox import PeerMailbox


def test_windows_peer_entry_points_fail_closed_without_private_acls(tmp_path, monkeypatch):
    credential = tmp_path / "peer-token"
    credential.write_text("secret", encoding="ascii")
    monkeypatch.setattr(peer_cli.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="private file ACLs"):
        peer_cli._publish_credential(tmp_path / "new-token")
    with pytest.raises(RuntimeError, match="private file ACLs"):
        peer_cli._credential(credential)
    with pytest.raises(RuntimeError, match="private file ACLs"):
        peer_cli.main()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific fail-closed guard")
def test_windows_peer_enrollment_cli_refuses_unprotected_credential(tmp_path):
    credential = tmp_path / "peer-token"
    result = subprocess.run(
        [sys.executable, "-m", "anywhere_computer.peer_cli", "--state-dir",
         str(tmp_path / "mailbox"), "enroll", "--peer-id", "codex:task-1",
         "--owner", "owner", "--account", "account", "--project", "project",
         "--runtime", "codex", "--credential-file", str(credential)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "private file ACLs" in result.stderr
    assert not credential.exists()


def _provision(state, credential, peer, *, account="account"):
    completed = subprocess.run(
        [sys.executable, "-m", "anywhere_computer.peer_cli", "--state-dir", str(state),
         "enroll", "--peer-id", peer, "--owner", "owner", "--account", account,
         "--project", "project", "--runtime", peer.split(":")[0],
         "--credential-file", str(credential)],
        capture_output=True, text=True, check=True,
    )
    assert completed.stdout.strip() == str(credential)


@pytest.mark.skipif(sys.platform == "win32", reason="peer CLI requires private Windows file ACLs")
def test_provisioning_retry_after_credential_publish(tmp_path):
    state = tmp_path / "mailbox"
    credential = tmp_path / "peer-token"
    credential.write_text("t" * 43 + "\n", encoding="ascii")
    os.chmod(credential, 0o600)
    _provision(state, credential, "codex:task-1")
    _provision(state, credential, "codex:task-1")
    with PeerMailbox(state) as mailbox:
        assert mailbox.identity("t" * 43).peer_id == "codex:task-1"
    conflict = tmp_path / "conflicting-token"
    failed = subprocess.run(
        [sys.executable, "-m", "anywhere_computer.peer_cli", "--state-dir", str(state),
         "enroll", "--peer-id", "codex:task-1", "--owner", "owner", "--account", "other",
         "--project", "project", "--runtime", "codex", "--credential-file", str(conflict)],
        capture_output=True, text=True,
    )
    assert failed.returncode != 0
    # A complete orphan file may remain after a DB conflict; it is never active.
    assert len(conflict.read_text(encoding="ascii").strip()) >= 32


@pytest.mark.skipif(sys.platform == "win32", reason="peer CLI requires private Windows file ACLs")
def test_credential_publication_failure_leaves_no_partial_final(tmp_path, monkeypatch):
    final = tmp_path / "peer-token"

    def fail_link(_staged, _final):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(peer_cli.os, "link", fail_link)
    with pytest.raises(OSError, match="synthetic"):
        peer_cli._publish_credential(final)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="peer CLI requires private Windows file ACLs")
def test_heartbeat_failure_exits_with_open_stdin(tmp_path):
    state = tmp_path / "mailbox"
    credential = tmp_path / "peer-token"
    _provision(state, credential, "codex:task-1")
    code = """
import asyncio
import sys
from pathlib import Path
from anywhere_computer import peer_cli
from anywhere_computer.peer_mailbox import PeerMailbox
original_heartbeat = PeerMailbox.heartbeat
calls = 0
def heartbeat(self, token, *, lease_seconds=60):
    global calls
    calls += 1
    if calls == 2:
        raise OSError('synthetic renewal failure')
    return original_heartbeat(self, token, lease_seconds=lease_seconds)
PeerMailbox.heartbeat = heartbeat
original_sleep = asyncio.sleep
async def advance(_seconds):
    await original_sleep(0)
asyncio.sleep = advance
asyncio.run(peer_cli._serve(Path(sys.argv[1]), Path(sys.argv[2])))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(state), str(credential)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert process.wait(timeout=5) == 1
        assert b"Peer presence renewal failed" in process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin is not None:
            process.stdin.close()


@pytest.mark.skipif(sys.platform == "win32", reason="peer CLI requires private Windows file ACLs")
async def test_two_local_peer_mcp_processes_exchange_and_ack(tmp_path):
    state = tmp_path / "mailbox"
    codex = tmp_path / "codex-token"
    claude = tmp_path / "claude-token"
    foreign = tmp_path / "foreign-token"
    _provision(state, codex, "codex:task-1")
    _provision(state, claude, "claude:session-1")
    _provision(state, foreign, "claude:foreign", account="other-account")

    def params(credential, *, thread_id=None):
        args = [
            "-m", "anywhere_computer.peer_cli", "--state-dir", str(state),
            "serve", "--credential-file", str(credential),
        ]
        if thread_id is not None:
            args.extend(["--thread-id", thread_id])
        return StdioServerParameters(command=sys.executable, args=[
            *args,
        ])

    async with asyncio.timeout(15):
        async with stdio_client(params(codex)) as (codex_read, codex_write):
            async with ClientSession(codex_read, codex_write) as codex_client:
                await codex_client.initialize()
                async with stdio_client(params(claude, thread_id="old-thread")) as (
                    claude_read, claude_write,
                ):
                    async with ClientSession(claude_read, claude_write) as claude_client:
                        await claude_client.initialize()
                        names = {tool.name for tool in (await codex_client.list_tools()).tools}
                        assert names == {
                            "peer_identity", "peer_send", "peer_inbox", "peer_wait", "peer_ack",
                            "peer_history", "peer_diagnose",
                        }
                        assert "enroll" not in names
                        identity = await codex_client.call_tool("peer_identity", {})
                        assert identity.structuredContent["data"]["peer_id"] == "codex:task-1"
                        diagnostic = await codex_client.call_tool("peer_diagnose", {
                            "recipient": "claude:session-1", "expected_thread_id": "new-thread",
                        })
                        assert diagnostic.structuredContent["data"]["presence"] == "MATCH"
                        assert diagnostic.structuredContent["data"]["process"] == "MATCH"
                        assert diagnostic.structuredContent["data"]["thread"] == "MISMATCH"
                        assert diagnostic.structuredContent["data"]["model_turn"] == "UNKNOWN"
                        with pytest.raises(McpError, match="request_id"):
                            await codex_client.call_tool("peer_send", {
                                "recipient": "claude:session-1", "text": "must not be stored",
                            })
                        assert (await claude_client.call_tool("peer_inbox", {})) \
                            .structuredContent["data"]["messages"] == []
                        empty_wait = await claude_client.call_tool("peer_wait", {"wait_ms": 0})
                        assert empty_wait.structuredContent["data"] == {
                            "messages": [], "timed_out": True}
                        invalid_wait = await claude_client.call_tool(
                            "peer_wait", {"wait_ms": 10_001})
                        assert invalid_wait.isError
                        changed = await codex_client.call_tool("peer_send", {
                            "recipient": "claude:session-1", "text": "wrong thread",
                            "expected_thread_id": "new-thread", "request_id": "c" * 32,
                        })
                        assert changed.isError
                        assert "offline" in changed.structuredContent["error"]
                        waiting = asyncio.create_task(claude_client.call_tool(
                            "peer_wait", {"wait_ms": 2_000}))
                        await asyncio.sleep(0.05)
                        assert not waiting.done()
                        sent = await codex_client.call_tool("peer_send", {
                            "recipient": "claude:session-1", "text": "こんにちは from Codex",
                            "expected_thread_id": "old-thread",
                            "request_id": "a" * 32,
                        })
                        assert not sent.isError
                        assert sent.structuredContent["data"]["delivery_id"] == "a" * 32
                        delivered = await asyncio.wait_for(waiting, 2)
                        assert not delivered.isError
                        assert delivered.structuredContent["data"]["timed_out"] is False
                        assert delivered.structuredContent["data"]["messages"][0][
                            "delivery_id"] == "a" * 32
                        again = await codex_client.call_tool("peer_send", {
                            "recipient": "claude:session-1", "text": "こんにちは from Codex",
                            "expected_thread_id": "old-thread",
                            "request_id": "a" * 32,
                        })
                        assert again.structuredContent["data"] == sent.structuredContent["data"]
                        inbox = await claude_client.call_tool("peer_inbox", {})
                        assert len(inbox.structuredContent["data"]["messages"]) == 1
                        denied = await codex_client.call_tool("peer_ack", {
                            "delivery_id": "a" * 32,
                        })
                        assert denied.isError
                        acked = await claude_client.call_tool("peer_ack", {
                            "delivery_id": "a" * 32,
                        })
                        assert not acked.isError
                        assert acked.structuredContent["data"]["acknowledged_at"] is not None
                        empty = await claude_client.call_tool("peer_inbox", {})
                        assert empty.structuredContent["data"]["messages"] == []
                        unpresented = await codex_client.call_tool("peer_send", {
                            "recipient": "claude:session-1", "text": "known ID, no inbox",
                            "expected_thread_id": "old-thread", "request_id": "d" * 32,
                        })
                        assert not unpresented.isError
                        premature = await claude_client.call_tool("peer_ack", {
                            "delivery_id": "d" * 32,
                        })
                        assert premature.isError
                        assert premature.structuredContent["error"] == "Peer access denied"
                        pending = await claude_client.call_tool("peer_inbox", {})
                        assert pending.structuredContent["data"]["messages"][0][
                            "delivery_id"] == "d" * 32
                        assert not (await claude_client.call_tool("peer_ack", {
                            "delivery_id": "d" * 32,
                        })).isError
                        history = await claude_client.call_tool("peer_history", {"limit": 2})
                        assert len(history.structuredContent["data"]["messages"]) == 2
                        assert history.structuredContent["data"]["next_cursor"] is None
                        assert history.structuredContent["data"]["has_more"] is False
                offline = await codex_client.call_tool("peer_send", {
                    "recipient": "claude:session-1", "text": "after disconnect",
                    "request_id": "b" * 32,
                })
                assert offline.isError
                assert "offline" in offline.structuredContent["error"]
                async with stdio_client(params(foreign)) as (foreign_read, foreign_write):
                    async with ClientSession(foreign_read, foreign_write) as foreign_client:
                        await foreign_client.initialize()
                        mismatched = await codex_client.call_tool("peer_send", {
                            "recipient": "claude:foreign", "text": "wrong account",
                            "request_id": "c" * 32,
                        })
                        assert mismatched.isError
                        assert (await foreign_client.call_tool("peer_inbox", {})) \
                            .structuredContent["data"]["messages"] == []
