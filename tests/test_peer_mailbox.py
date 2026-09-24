"""Local mailbox boundaries; no provider or browser message is sent here."""

import sqlite3

import pytest

from anywhere_computer import peer_mailbox
from anywhere_computer.peer_mailbox import (
    MailboxFull,
    Peer,
    PeerAccessError,
    PeerMailbox,
    PeerOffline,
)


def test_exchange_duplicate_ack_and_unread_survive_restart(tmp_path):
    now = [1000.0]
    first = Peer("codex:task-1", "owner", "account", "project", "codex")
    second = Peer("claude:session-1", "owner", "account", "project", "claude")
    with PeerMailbox(tmp_path, clock=lambda: now[0]) as mailbox:
        codex_token = mailbox.enroll(first)
        claude_token = mailbox.enroll(second)
        mailbox.heartbeat(claude_token)
        sent = mailbox.send(codex_token, recipient=second.peer_id,
                            delivery_id="delivery-1", text="Hello, peer")
        assert sent.acknowledged_at is None
        assert mailbox.send(codex_token, recipient=second.peer_id,
                            delivery_id="delivery-1", text="Hello, peer") == sent
        with pytest.raises(ValueError, match="different arguments"):
            mailbox.send(codex_token, recipient=second.peer_id,
                         delivery_id="delivery-1", text="changed")
    with PeerMailbox(tmp_path, clock=lambda: now[0]) as reopened:
        assert reopened.inbox(claude_token) == [sent]
        assert reopened.inbox(codex_token) == []
        with pytest.raises(PeerAccessError):
            reopened.acknowledge(codex_token, sent.delivery_id)
        acknowledged = reopened.acknowledge(claude_token, sent.delivery_id)
        assert acknowledged.acknowledged_at == now[0]
        assert reopened.acknowledge(claude_token, sent.delivery_id) == acknowledged
        assert reopened.inbox(claude_token) == []
        assert reopened.history(codex_token) == [acknowledged]


def test_account_project_owner_and_offline_boundaries(tmp_path):
    now = [1000.0]
    with PeerMailbox(tmp_path, clock=lambda: now[0]) as mailbox:
        source = mailbox.enroll(Peer("source", "owner", "account", "project", "codex"))
        same = mailbox.enroll(Peer("same", "owner", "account", "project", "claude"))
        other_account = mailbox.enroll(Peer("other-account", "owner", "other", "project", "claude"))
        other_project = mailbox.enroll(Peer("other-project", "owner", "account", "other", "claude"))
        other_owner = mailbox.enroll(Peer("other-owner", "other", "account", "project", "claude"))
        for token in (same, other_account, other_project, other_owner):
            mailbox.heartbeat(token, lease_seconds=5)
        for recipient in ("other-account", "other-project", "other-owner", "unknown"):
            with pytest.raises(PeerAccessError, match="unavailable"):
                mailbox.send(source, recipient=recipient, delivery_id="delivery-1", text="text")
        with pytest.raises(PeerAccessError, match="credential"):
            mailbox.inbox("invalid")
        now[0] += 6
        with pytest.raises(PeerOffline, match="offline"):
            mailbox.send(source, recipient="same", delivery_id="delivery-1", text="text")
        assert mailbox.inbox(same) == []
        mailbox.heartbeat(same)
        assert mailbox.send(source, recipient="same", delivery_id="delivery-1",
                            text="text").recipient == "same"


def test_message_bounds_and_no_peer_self_registration_via_runtime_methods(tmp_path):
    with PeerMailbox(tmp_path) as mailbox:
        sender = mailbox.enroll(Peer("sender", "owner", "account", "project", "parent"))
        recipient = mailbox.enroll(Peer("recipient", "owner", "account", "project", "subchat"))
        mailbox.heartbeat(recipient)
        with pytest.raises(ValueError, match="UTF-8"):
            mailbox.send(sender, recipient="recipient", delivery_id="d-1", text="あ" * 6000)
        with pytest.raises(ValueError, match="Invalid"):
            mailbox.enroll(Peer("bad peer", "owner", "account", "project", "codex"))
        with pytest.raises(sqlite3.IntegrityError):
            mailbox.enroll(Peer("sender", "owner", "account", "project", "parent"))
        assert mailbox.inbox(recipient) == []


def test_two_live_instances_and_bounded_mailbox(tmp_path, monkeypatch):
    monkeypatch.setattr(peer_mailbox, "MAX_PENDING_PER_PEER", 2)
    with PeerMailbox(tmp_path) as admin:
        sender = admin.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = admin.enroll(Peer("recipient", "owner", "account", "project", "claude"))
    with PeerMailbox(tmp_path) as first, PeerMailbox(tmp_path) as second:
        first.heartbeat(recipient)
        second.heartbeat(recipient)
        first.disconnect(recipient)
        one = first.send(sender, recipient="recipient", delivery_id="d1", text="first")
        first.send(sender, recipient="recipient", delivery_id="d2", text="second")
        with pytest.raises(MailboxFull):
            first.send(sender, recipient="recipient", delivery_id="d3", text="third")
        assert first.send(sender, recipient="recipient", delivery_id="d1", text="first") == one
        second.acknowledge(recipient, "d1")
        first.send(sender, recipient="recipient", delivery_id="d3", text="third")
        with pytest.raises(MailboxFull):
            first.send(sender, recipient="recipient", delivery_id="d4", text="fourth")
        second.acknowledge(recipient, "d2")
        first.send(sender, recipient="recipient", delivery_id="d4", text="fourth")
        second.disconnect(recipient)
        with pytest.raises(PeerOffline):
            first.send(sender, recipient="recipient", delivery_id="d5", text="fifth")


def test_acknowledged_history_is_bounded_and_pruned_ids_can_be_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(peer_mailbox, "MAX_RETAINED_ACKED_PER_PEER", 2)
    with PeerMailbox(tmp_path) as mailbox:
        sender = mailbox.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = mailbox.enroll(Peer("recipient", "owner", "account", "project", "claude"))
        mailbox.heartbeat(recipient)
        for number in range(10):
            delivery_id = f"delivery-{number}"
            mailbox.send(sender, recipient="recipient", delivery_id=delivery_id,
                         text=f"message {number}")
            mailbox.acknowledge(recipient, delivery_id)
            assert mailbox.connection.execute(
                "SELECT count(*) FROM peer_messages WHERE recipient='recipient'"
            ).fetchone()[0] <= 2
        assert [item.delivery_id for item in mailbox.history(recipient)] == [
            "delivery-9", "delivery-8",
        ]
        with pytest.raises(PeerAccessError):
            mailbox.acknowledge(recipient, "delivery-0")
        # Once its acknowledged record is pruned, an old ID is no longer reserved.
        assert mailbox.send(sender, recipient="recipient", delivery_id="delivery-0",
                            text="new after retention").text == "new after retention"
