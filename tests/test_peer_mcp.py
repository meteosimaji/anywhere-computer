"""Two independent local MCP processes exchange a saved peer message."""

import asyncio
import os
import subprocess
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from anywhere_computer import peer_cli
from anywhere_computer.peer_mailbox import PeerMailbox


def _provision(state, credential, peer, *, account="account"):
    completed = subprocess.run(
        [sys.executable, "-m", "anywhere_computer.peer_cli", "--state-dir", str(state),
         "enroll", "--peer-id", peer, "--owner", "owner", "--account", account,
         "--project", "project", "--runtime", peer.split(":")[0],
         "--credential-file", str(credential)],
        capture_output=True, text=True, check=True,
    )
    assert completed.stdout.strip() == str(credential)


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


def test_credential_publication_failure_leaves_no_partial_final(tmp_path, monkeypatch):
    final = tmp_path / "peer-token"

    def fail_link(_staged, _final):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(peer_cli.os, "link", fail_link)
    with pytest.raises(OSError, match="synthetic"):
        peer_cli._publish_credential(final)
    assert list(tmp_path.iterdir()) == []


async def test_two_local_peer_mcp_processes_exchange_and_ack(tmp_path):
    state = tmp_path / "mailbox"
    codex = tmp_path / "codex-token"
    claude = tmp_path / "claude-token"
    foreign = tmp_path / "foreign-token"
    _provision(state, codex, "codex:task-1")
    _provision(state, claude, "claude:session-1")
    _provision(state, foreign, "claude:foreign", account="other-account")

    def params(credential):
        return StdioServerParameters(command=sys.executable, args=[
            "-m", "anywhere_computer.peer_cli", "--state-dir", str(state),
            "serve", "--credential-file", str(credential),
        ])

    async with asyncio.timeout(15):
        async with stdio_client(params(codex)) as (codex_read, codex_write):
            async with ClientSession(codex_read, codex_write) as codex_client:
                await codex_client.initialize()
                async with stdio_client(params(claude)) as (claude_read, claude_write):
                    async with ClientSession(claude_read, claude_write) as claude_client:
                        await claude_client.initialize()
                        names = {tool.name for tool in (await codex_client.list_tools()).tools}
                        assert names == {
                            "peer_identity", "peer_send", "peer_inbox", "peer_ack",
                            "peer_history",
                        }
                        assert "enroll" not in names
                        identity = await codex_client.call_tool("peer_identity", {})
                        assert identity.structuredContent["data"]["peer_id"] == "codex:task-1"
                        sent = await codex_client.call_tool("peer_send", {
                            "recipient": "claude:session-1", "text": "こんにちは from Codex",
                            "request_id": "a" * 32,
                        })
                        assert not sent.isError
                        assert sent.structuredContent["data"]["delivery_id"] == "a" * 32
                        again = await codex_client.call_tool("peer_send", {
                            "recipient": "claude:session-1", "text": "こんにちは from Codex",
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
