"""Local mailbox boundaries; no provider or browser message is sent here."""

import hashlib
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
        reopened.heartbeat(claude_token)
        reopened.heartbeat(codex_token)
        assert reopened.inbox(claude_token) == [sent]
        assert reopened.inbox(codex_token) == []
        with pytest.raises(PeerAccessError):
            reopened.acknowledge(codex_token, sent.delivery_id)
        acknowledged = reopened.acknowledge(claude_token, sent.delivery_id)
        assert acknowledged.acknowledged_at == now[0]
        assert reopened.acknowledge(claude_token, sent.delivery_id) == acknowledged
        assert reopened.inbox(claude_token) == []
        assert reopened.history(codex_token) == [acknowledged]


def test_ack_requires_presentation_by_current_instance_after_restart(tmp_path):
    with PeerMailbox(tmp_path) as sender_box, PeerMailbox(tmp_path) as recipient_box:
        sender = sender_box.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = sender_box.enroll(Peer("recipient", "owner", "account", "project", "claude"))
        recipient_box.bind(session_id="session", thread_id="thread")
        recipient_box.heartbeat(recipient)
        sent = sender_box.send(sender, recipient="recipient", delivery_id="known-id",
                               text="message", expected_session_id="session",
                               expected_thread_id="thread")
        with pytest.raises(PeerAccessError, match="not been presented"):
            recipient_box.acknowledge(recipient, sent.delivery_id)
        assert recipient_box.inbox(recipient) == [sent]
        assert recipient_box.connection.execute(
            "SELECT presented_at FROM peer_presentations WHERE recipient=? AND delivery_id=?",
            ("recipient", sent.delivery_id),
        ).fetchone() is not None
    with PeerMailbox(tmp_path) as recovered:
        recovered.bind(session_id="session", thread_id="thread")
        recovered.heartbeat(recipient)
        with pytest.raises(PeerAccessError, match="not been presented"):
            recovered.acknowledge(recipient, sent.delivery_id)
        assert recovered.inbox(recipient) == [sent]
        assert recovered.acknowledge(recipient, sent.delivery_id).acknowledged_at is not None
        assert recovered.acknowledge(recipient, sent.delivery_id).acknowledged_at is not None


def test_history_pages_recover_all_retained_messages_after_restart(tmp_path):
    with PeerMailbox(tmp_path, clock=lambda: 1000.0) as mailbox:
        sender = mailbox.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = mailbox.enroll(Peer("recipient", "owner", "account", "project", "claude"))
        mailbox.heartbeat(recipient)
        for number in range(101):
            delivery_id = f"delivery-{number:03}"
            mailbox.send(sender, recipient="recipient", delivery_id=delivery_id,
                         text=f"message {number}")
            mailbox.inbox(recipient)
            mailbox.acknowledge(recipient, delivery_id)
    with PeerMailbox(tmp_path, clock=lambda: 1000.0) as reopened:
        reopened.heartbeat(recipient)
        seen = []
        cursor = None
        while True:
            page = reopened.history_page(recipient, limit=40, cursor=cursor)
            seen.extend(message.delivery_id for message in page.messages)
            assert page.has_more == (page.next_cursor is not None)
            if not page.has_more:
                break
            cursor = page.next_cursor
        assert seen == [f"delivery-{number:03}" for number in range(100, -1, -1)]
        with pytest.raises(ValueError, match="cursor"):
            reopened.history_page(recipient, cursor="ph1.invalid")


def test_history_cursor_orders_shared_ids_and_rejects_other_identity(tmp_path):
    with PeerMailbox(tmp_path, clock=lambda: 1000.0) as mailbox:
        sender = mailbox.enroll(Peer("sender", "owner", "account", "project", "codex"))
        first = mailbox.enroll(Peer("first", "owner", "account", "project", "claude"))
        second = mailbox.enroll(Peer("second", "owner", "account", "project", "claude"))
        mailbox.heartbeat(sender)
        mailbox.heartbeat(first)
        mailbox.heartbeat(second)
        mailbox.send(sender, recipient="first", delivery_id="shared", text="first")
        mailbox.send(sender, recipient="second", delivery_id="shared", text="second")
        page = mailbox.history_page(sender, limit=1)
        assert page.has_more and page.next_cursor
        older = mailbox.history_page(sender, limit=1, cursor=page.next_cursor)
        assert {page.messages[0].recipient, older.messages[0].recipient} == {"first", "second"}
        assert not older.has_more
        with pytest.raises(ValueError, match="cursor"):
            mailbox.history_page(first, cursor=page.next_cursor)


def test_history_cursor_rejects_replacement_session(tmp_path):
    with PeerMailbox(tmp_path, clock=lambda: 1000.0) as first:
        sender = first.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = first.enroll(Peer("recipient", "owner", "account", "project", "claude"))
        first.bind(session_id="session-a", thread_id="thread-a")
        first.heartbeat(recipient)
        for number in range(2):
            first.send(sender, recipient="recipient", delivery_id=f"message-{number}",
                       text="text")
        cursor = first.history_page(recipient, limit=1).next_cursor
        assert cursor
    with PeerMailbox(tmp_path, clock=lambda: 1000.0) as replacement:
        replacement.bind(session_id="session-b", thread_id="thread-b")
        replacement.heartbeat(recipient)
        with pytest.raises(PeerAccessError, match="another session"):
            replacement.history_page(recipient, cursor=cursor)


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
        # An already accepted ID cannot reveal another account's recipient.
        mailbox.send(other_account, recipient="other-account", delivery_id="foreign-id",
                     text="foreign text")
        with pytest.raises(PeerAccessError, match="unavailable"):
            mailbox.send(source, recipient="other-account", delivery_id="foreign-id",
                         text="foreign text")
        with pytest.raises(PeerAccessError, match="credential"):
            mailbox.inbox("invalid")
        now[0] += 6
        with pytest.raises(PeerOffline, match="offline"):
            mailbox.send(source, recipient="same", delivery_id="delivery-1", text="text")
        with pytest.raises(PeerOffline, match="sole live recipient"):
            mailbox.inbox(same)
        mailbox.heartbeat(same)
        assert mailbox.inbox(same) == []
        assert mailbox.send(source, recipient="same", delivery_id="delivery-1",
                            text="text").recipient == "same"


def test_delivery_ids_are_scoped_to_recipient_and_account_project(tmp_path, monkeypatch):
    monkeypatch.setattr(peer_mailbox, "MAX_RETAINED_ACKED_PER_PEER", 1)
    with PeerMailbox(tmp_path) as mailbox:
        senders = {}
        recipients = {}
        for project in ("first", "second"):
            senders[project] = mailbox.enroll(Peer(
                "sender-" + project, "owner", "account", project, "codex"))
            recipients[project] = mailbox.enroll(Peer(
                "recipient-" + project, "owner", "account", project, "claude"))
            mailbox.heartbeat(recipients[project])
        first = mailbox.send(senders["first"], recipient="recipient-first",
                             delivery_id="shared-id", text="first message")
        second = mailbox.send(senders["second"], recipient="recipient-second",
                              delivery_id="shared-id", text="second message")
        assert first.text != second.text
        assert mailbox.inbox(recipients["first"]) == [first]
        assert mailbox.inbox(recipients["second"]) == [second]
        assert mailbox.acknowledge(recipients["first"], "shared-id").text == first.text
        assert mailbox.inbox(recipients["second"]) == [second]
        assert mailbox.acknowledge(recipients["second"], "shared-id").text == second.text
        mailbox.send(senders["first"], recipient="recipient-first",
                     delivery_id="next-id", text="force prune")
        mailbox.inbox(recipients["first"])
        mailbox.acknowledge(recipients["first"], "next-id")
        with pytest.raises(ValueError, match="history was pruned"):
            mailbox.send(senders["first"], recipient="recipient-first",
                         delivery_id="shared-id", text="must not replay")
        retried = mailbox.send(senders["second"], recipient="recipient-second",
                               delivery_id="shared-id", text="second message")
        assert retried == mailbox.acknowledge(recipients["second"], "shared-id")


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
        second.inbox(recipient)
        second.acknowledge(recipient, "d1")
        first.send(sender, recipient="recipient", delivery_id="d3", text="third")
        with pytest.raises(MailboxFull):
            first.send(sender, recipient="recipient", delivery_id="d4", text="fourth")
        second.inbox(recipient)
        second.acknowledge(recipient, "d2")
        first.send(sender, recipient="recipient", delivery_id="d4", text="fourth")
        second.disconnect(recipient)
        with pytest.raises(PeerOffline):
            first.send(sender, recipient="recipient", delivery_id="d5", text="fifth")


def test_old_instance_cannot_read_or_ack_after_peer_handoff(tmp_path):
    with PeerMailbox(tmp_path) as admin:
        sender = admin.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = admin.enroll(Peer("recipient", "owner", "account", "project", "claude"))
    with PeerMailbox(tmp_path) as old, PeerMailbox(tmp_path) as current:
        old.heartbeat(recipient)
        sent = old.send(sender, recipient="recipient", delivery_id="handoff-1",
                        text="private handoff message")
        current.heartbeat(recipient)
        for mailbox in (old, current):
            with pytest.raises(PeerOffline, match="sole live recipient"):
                mailbox.inbox(recipient)
            with pytest.raises(PeerOffline, match="sole live recipient"):
                mailbox.acknowledge(recipient, sent.delivery_id)
        old.disconnect(recipient)
        with pytest.raises(PeerOffline, match="sole live recipient"):
            old.history(recipient)
        with pytest.raises(PeerOffline, match="sole live recipient"):
            old.acknowledge(recipient, sent.delivery_id)
        assert current.inbox(recipient) == [sent]
        assert current.acknowledge(recipient, sent.delivery_id).acknowledged_at is not None


def test_thread_bound_message_is_not_consumed_by_replacement_thread(tmp_path):
    with PeerMailbox(tmp_path) as sender_box, PeerMailbox(tmp_path) as first:
        sender = sender_box.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = sender_box.enroll(Peer("recipient", "owner", "account", "project", "claude"))
        first.bind(session_id="session-a", thread_id="thread-a")
        first.heartbeat(recipient)
        sent = sender_box.send(sender, recipient="recipient", delivery_id="bound-thread",
                               text="for thread a", expected_session_id="session-a",
                               expected_thread_id="thread-a")
        with pytest.raises(ValueError, match="different arguments"):
            sender_box.send(sender, recipient="recipient", delivery_id="bound-thread",
                            text="for thread a", expected_thread_id="thread-b")
    with PeerMailbox(tmp_path) as replacement:
        replacement.bind(session_id="session-b", thread_id="thread-b")
        replacement.heartbeat(recipient)
        assert replacement.inbox(recipient) == []
        assert replacement.history(recipient) == []
        with pytest.raises(PeerAccessError, match="session"):
            replacement.acknowledge(recipient, sent.delivery_id)
        assert replacement.connection.execute(
            "SELECT acknowledged_at FROM peer_messages WHERE delivery_id=?",
            (sent.delivery_id,),
        ).fetchone() == (None,)
    with PeerMailbox(tmp_path) as recovered:
        recovered.bind(session_id="session-a", thread_id="thread-a")
        recovered.heartbeat(recipient)
        assert recovered.inbox(recipient) == [sent]
        assert recovered.acknowledge(recipient, sent.delivery_id).acknowledged_at is not None


def test_unknown_competing_presence_blocks_send_and_consumption(tmp_path):
    with PeerMailbox(tmp_path) as sender_box, PeerMailbox(tmp_path) as recipient_box:
        sender = sender_box.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = sender_box.enroll(Peer("recipient", "owner", "account", "project", "claude"))
        recipient_box.heartbeat(recipient)
        sent = sender_box.send(sender, recipient="recipient", delivery_id="before-unknown",
                               text="already accepted")
        with sender_box.connection:
            sender_box.connection.execute(
                "INSERT INTO peer_presence(peer_id,instance_id,online_until) VALUES(?,?,?)",
                ("recipient", "unknown-instance", recipient_box._clock() + 60),
            )
        with pytest.raises(PeerOffline, match="uncertain"):
            sender_box.send(sender, recipient="recipient", delivery_id="during-unknown",
                            text="must not accept")
        for operation in (lambda: recipient_box.inbox(recipient),
                          lambda: recipient_box.history(recipient),
                          lambda: recipient_box.acknowledge(recipient, sent.delivery_id)):
            with pytest.raises(PeerOffline, match="sole live recipient"):
                operation()
        with sender_box.connection:
            sender_box.connection.execute(
                "DELETE FROM peer_presence WHERE instance_id='unknown-instance'"
            )
        assert recipient_box.inbox(recipient) == [sent]
        assert recipient_box.acknowledge(recipient, sent.delivery_id).acknowledged_at is not None


def test_acknowledged_history_is_bounded_and_pruned_ids_stay_reserved(tmp_path, monkeypatch):
    monkeypatch.setattr(peer_mailbox, "MAX_RETAINED_ACKED_PER_PEER", 2)
    with PeerMailbox(tmp_path) as mailbox:
        sender = mailbox.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = mailbox.enroll(Peer("recipient", "owner", "account", "project", "claude"))
        mailbox.heartbeat(recipient)
        for number in range(10):
            delivery_id = f"delivery-{number}"
            mailbox.send(sender, recipient="recipient", delivery_id=delivery_id,
                         text=f"message {number}")
            mailbox.inbox(recipient)
            mailbox.acknowledge(recipient, delivery_id)
            assert mailbox.connection.execute(
                "SELECT count(*) FROM peer_messages WHERE recipient='recipient'"
            ).fetchone()[0] <= 2
        assert [item.delivery_id for item in mailbox.history(recipient)] == [
            "delivery-9", "delivery-8",
        ]
        with pytest.raises(PeerAccessError):
            mailbox.acknowledge(recipient, "delivery-0")
        with pytest.raises(ValueError, match="already used"):
            mailbox.send(sender, recipient="recipient", delivery_id="delivery-0",
                         text="new after retention")
    with PeerMailbox(tmp_path) as reopened:
        reopened.heartbeat(recipient)
        with pytest.raises(ValueError, match="already used"):
            reopened.send(sender, recipient="recipient", delivery_id="delivery-0",
                          text="new after restart")


def test_read_only_diagnosis_distinguishes_process_claim_and_model_turn(tmp_path):
    now = [1000.0]
    with PeerMailbox(tmp_path, clock=lambda: now[0]) as sender_box, \
            PeerMailbox(tmp_path, clock=lambda: now[0]) as recipient_box:
        sender = sender_box.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = sender_box.enroll(Peer("recipient", "owner", "account", "project", "claude"))
        unknown = sender_box.diagnose(sender, recipient="recipient",
                                      expected_thread_id="thread-2")
        assert unknown.presence == "MISMATCH"
        assert unknown.model_turn == "UNKNOWN"
        recipient_box.bind(session_id="session-1", thread_id="thread-1")
        recipient_box.heartbeat(recipient, lease_seconds=5)
        matched = sender_box.diagnose(sender, recipient="recipient",
                                      expected_session_id="session-1",
                                      expected_thread_id="thread-1")
        assert (matched.presence, matched.process, matched.session, matched.thread) == (
            "MATCH", "MATCH", "MATCH", "MATCH")
        assert matched.model_turn == "UNKNOWN"
        changed = sender_box.diagnose(sender, recipient="recipient",
                                      expected_thread_id="thread-2")
        assert changed.thread == "MISMATCH"
        assert changed.session == "UNKNOWN"
        with sender_box.connection:
            sender_box.connection.execute(
                "UPDATE peer_presence SET process_started=0 WHERE peer_id='recipient'"
            )
        assert sender_box.diagnose(sender, recipient="recipient").process == "MISMATCH"
        with pytest.raises(PeerOffline, match="no message was accepted"):
            sender_box.send(sender, recipient="recipient",
                            delivery_id="stale-process", text="must not accept")
        with pytest.raises(PeerOffline, match="sole live recipient"):
            recipient_box.inbox(recipient)
        recipient_box.heartbeat(recipient, lease_seconds=5)
        assert sender_box.send(sender, recipient="recipient",
                               delivery_id="live-process", text="accepted").text == "accepted"
        with pytest.raises(PeerAccessError):
            sender_box.diagnose(sender, recipient="foreign")
        now[0] += 6
        assert sender_box.diagnose(sender, recipient="recipient").presence == "MISMATCH"


def test_diagnosis_does_not_pick_one_of_two_live_destinations(tmp_path):
    with PeerMailbox(tmp_path) as first, PeerMailbox(tmp_path) as second:
        caller = first.enroll(Peer("caller", "owner", "account", "project", "codex"))
        target = first.enroll(Peer("target", "owner", "account", "project", "claude"))
        first.bind(thread_id="old-thread")
        second.bind(thread_id="new-thread")
        first.heartbeat(target)
        second.heartbeat(target)
        diagnostic = first.diagnose(caller, recipient="target",
                                    expected_thread_id="new-thread")
        assert (diagnostic.presence, diagnostic.process, diagnostic.thread) == (
            "UNKNOWN", "UNKNOWN", "UNKNOWN")


def test_diagnosis_ignores_confirmed_dead_competitor_but_not_unknown(tmp_path):
    with PeerMailbox(tmp_path) as sender_box, PeerMailbox(tmp_path) as recipient_box:
        sender = sender_box.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = sender_box.enroll(Peer("recipient", "owner", "account", "project", "claude"))
        recipient_box.bind(session_id="current-session", thread_id="current-thread")
        recipient_box.heartbeat(recipient)
        with sender_box.connection:
            sender_box.connection.execute(
                "INSERT INTO peer_presence(peer_id,instance_id,online_until,pid,process_started) "
                "VALUES(?,?,?,?,?)",
                ("recipient", "dead-instance", recipient_box._clock() + 60, 999999999, 0.0),
            )
        diagnostic = sender_box.diagnose(sender, recipient="recipient",
                                         expected_thread_id="current-thread")
        assert (diagnostic.presence, diagnostic.process, diagnostic.thread) == (
            "MATCH", "MATCH", "MATCH")
        assert sender_box.send(sender, recipient="recipient", delivery_id="dead-competitor",
                               text="deliver").text == "deliver"
        with sender_box.connection:
            sender_box.connection.execute(
                "UPDATE peer_presence SET pid=NULL,process_started=NULL "
                "WHERE instance_id='dead-instance'")
        assert sender_box.diagnose(sender, recipient="recipient").presence == "UNKNOWN"


def test_send_expected_destination_rejects_changed_or_ambiguous_thread(tmp_path):
    with PeerMailbox(tmp_path) as sender_box, PeerMailbox(tmp_path) as recipient_box, \
            PeerMailbox(tmp_path) as another_box:
        sender = sender_box.enroll(Peer("sender", "owner", "account", "project", "codex"))
        recipient = sender_box.enroll(Peer("recipient", "owner", "account", "project", "claude"))
        recipient_box.bind(session_id="session-1", thread_id="thread-1")
        recipient_box.heartbeat(recipient)
        assert sender_box.diagnose(sender, recipient="recipient",
                                   expected_session_id="session-1",
                                   expected_thread_id="thread-1").thread == "MATCH"
        recipient_box.bind(session_id="session-1", thread_id="thread-2")
        recipient_box.heartbeat(recipient)
        with pytest.raises(PeerOffline, match="changed"):
            sender_box.send(sender, recipient="recipient", delivery_id="stale-thread",
                            text="do not deliver", expected_session_id="session-1",
                            expected_thread_id="thread-1")
        assert recipient_box.inbox(recipient) == []
        another_box.bind(session_id="session-1", thread_id="thread-3")
        another_box.heartbeat(recipient)
        with pytest.raises(PeerOffline, match="multiple live sessions"):
            sender_box.send(sender, recipient="recipient", delivery_id="ambiguous-unpinned",
                            text="do not deliver")
        with pytest.raises(PeerOffline, match="multiple live sessions"):
            sender_box.send(sender, recipient="recipient", delivery_id="ambiguous-thread",
                            text="do not deliver", expected_session_id="session-1")
        with pytest.raises(PeerOffline, match="sole live recipient"):
            recipient_box.inbox(recipient)
        another_box.disconnect(recipient)
        accepted = sender_box.send(sender, recipient="recipient", delivery_id="current-thread",
                                   text="deliver", expected_session_id="session-1",
                                   expected_thread_id="thread-2")
        assert accepted.text == "deliver"


def test_existing_presence_schema_migrates_without_claiming_process_evidence(tmp_path):
    database = tmp_path / "peer_mailbox.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE peer_presence (peer_id TEXT NOT NULL, instance_id TEXT NOT NULL, "
            "online_until REAL NOT NULL, PRIMARY KEY(peer_id,instance_id))"
        )
    with PeerMailbox(tmp_path) as mailbox:
        columns = {row[1] for row in mailbox.connection.execute(
            "PRAGMA table_info(peer_presence)")}
        assert {"pid", "process_started", "session_id", "thread_id"} <= columns


def test_existing_message_schema_migrates_to_optional_destination_labels(tmp_path):
    database = tmp_path / "peer_mailbox.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE peer_messages (delivery_id TEXT PRIMARY KEY, sender TEXT NOT NULL, "
            "recipient TEXT NOT NULL, text TEXT NOT NULL, sent_at REAL NOT NULL, "
            "acknowledged_at REAL)"
        )
    with PeerMailbox(tmp_path) as mailbox:
        columns = {row[1] for row in mailbox.connection.execute(
            "PRAGMA table_info(peer_messages)")}
        assert {"expected_session_id", "expected_thread_id"} <= columns
        keys = {row[1]: row[5] for row in mailbox.connection.execute(
            "PRAGMA table_info(peer_messages)")}
        assert (keys["recipient"], keys["delivery_id"]) == (1, 2)


def test_migration_preserves_existing_messages_and_legacy_tombstones(tmp_path):
    database = tmp_path / "peer_mailbox.sqlite3"
    sender_token = "s" * 32
    recipient_token = "r" * 32
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE peers (peer_id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
            "account TEXT NOT NULL, project TEXT NOT NULL, runtime TEXT NOT NULL, "
            "token_hash TEXT NOT NULL UNIQUE)"
        )
        connection.execute(
            "CREATE TABLE peer_messages (delivery_id TEXT PRIMARY KEY, "
            "sender TEXT NOT NULL, recipient TEXT NOT NULL, text TEXT NOT NULL, "
            "sent_at REAL NOT NULL, acknowledged_at REAL)"
        )
        connection.execute(
            "CREATE TABLE peer_delivery_tombstones (delivery_id TEXT PRIMARY KEY)"
        )
        for peer_id, runtime, token in (("sender", "codex", sender_token),
                                        ("recipient", "claude", recipient_token)):
            connection.execute("INSERT INTO peers VALUES (?,?,?,?,?,?)", (
                peer_id, "owner", "account", "project", runtime,
                hashlib.sha256(token.encode()).hexdigest(),
            ))
        connection.execute(
            "INSERT INTO peer_messages VALUES (?,?,?,?,?,?)",
            ("old-id", "sender", "recipient", "before migration", 123.0, None),
        )
        connection.execute(
            "INSERT INTO peer_delivery_tombstones VALUES ('pruned-id')"
        )
    with PeerMailbox(tmp_path) as mailbox:
        mailbox.heartbeat(recipient_token)
        with pytest.raises(ValueError, match="before mailbox migration"):
            mailbox.send(sender_token, recipient="recipient", delivery_id="pruned-id",
                         text="must not replay")
        saved = mailbox.send(sender_token, recipient="recipient", delivery_id="new-id",
                             text="after migration")
        assert [message.delivery_id for message in mailbox.inbox(recipient_token)] == [
            "old-id", "new-id",
        ]
    with PeerMailbox(tmp_path) as reopened:
        reopened.heartbeat(recipient_token)
        assert [message.delivery_id for message in reopened.inbox(recipient_token)] == [
            "old-id", saved.delivery_id,
        ]
