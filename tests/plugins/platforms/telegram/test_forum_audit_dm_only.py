"""User messages are never copied to the audit forum, in any scope.

Inbound forwarding is disabled: Hermes does not store or forward user
message content to the logs channel. The audit forum carries operational
error notices only. These tests pin that boundary, not the old copy path.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from telethon import types

from plugins.platforms.telegram.forum_audit import (
    _db, _insert, is_private_transcript,
)
from plugins.platforms.telegram.forum_audit_transport import ForumAuditTransport
from tests.plugins.platforms.telegram.test_forum_audit import configured
from tests.plugins.platforms.telegram.test_forum_audit_transport import _FakeAdapter


@pytest.mark.parametrize("role,uid,allowed", [("owner", 42, True), ("allowed", 43, True), ("unlisted", 44, False)])
@pytest.mark.parametrize("scope,chat_type,chat_id,thread,peer", [
    ("private", "dm", 42, None, types.PeerUser(42)),
    ("private thread", "dm", 42, "77", types.PeerUser(42)),
    ("basic group", "group", -81, None, types.PeerChat(81)),
    ("supergroup", "supergroup", -1000000000081, None, types.PeerChannel(81)),
    ("forum General", "group", -1000000000081, "1", types.PeerChannel(81)),
    ("forum named topic", "group", -1000000000081, "77", types.PeerChannel(81)),
    ("channel", "channel", -1000000000081, None, types.PeerChannel(81)),
])
@pytest.mark.parametrize("trigger", ["ordinary", "mention", "reply", "allowall", "allowlist"])
def test_no_private_dm_is_copied_to_the_audit_forum(tmp_path, role, uid, allowed,
                                                    scope, chat_type, chat_id, thread, peer, trigger):
    adapter = _FakeAdapter(tmp_path)
    if scope.startswith("private"):
        chat_id, peer = uid, types.PeerUser(uid)
    adapter.authorized = allowed or trigger == "allowall"
    adapter.explicit = allowed
    controller = ForumAuditTransport(adapter, tmp_path)
    controller._service = configured(tmp_path)
    source = SimpleNamespace(chat_id=str(chat_id), user_id=str(uid), chat_type=chat_type,
                             thread_id=thread, is_bot=False, profile_route_rejected=False)
    message = SimpleNamespace(id=101, peer_id=peer, message="private test content",
                              mentioned=trigger == "mention", is_reply=trigger == "reply")
    sent = []

    async def send(*args):
        sent.append(args)

    async def scenario():
        await adapter._process_inbound_event({"source": source}, [message])
        assert controller._service.status()["pending"] == 0
        assert await controller._service.drain(send_text=send) == 0
        assert controller._service.user_topic(uid) is None

    asyncio.run(scenario())
    assert adapter.original_calls == ["trigger-filter-and-routing"]
    assert sent == []  # Nothing is rerouted to General/Operations either.


@pytest.mark.parametrize("chat_type,chat_id,thread,peer", [
    ("dm", 42, None, types.PeerChat(42)),
    ("dm", 42, None, types.PeerChannel(42)),
    ("dm", 42, None, types.PeerUser(43)),
    ("dm", 42, None, None),
    ("dm", -42, None, types.PeerUser(42)),
    ("dm", 42, "bad", types.PeerUser(42)),
    (None, 42, None, types.PeerUser(42)),
    ([], 42, None, types.PeerUser(42)),
    ("unknown", 42, None, types.PeerUser(42)),
])
def test_unknown_or_contradictory_telegram_scope_never_enters_audit(tmp_path, chat_type, chat_id, thread, peer):
    assert not is_private_transcript(chat_type, chat_id, thread, peer=peer)
    adapter = _FakeAdapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    controller._service = configured(tmp_path)
    source = SimpleNamespace(chat_type=chat_type, chat_id=str(chat_id), thread_id=thread,
                             user_id="42", is_bot=False)
    asyncio.run(adapter._process_inbound_event({"source": source}, [SimpleNamespace(id=9, peer_id=peer)]))
    assert controller._service.status()["pending"] == 0
    assert adapter.original_calls == ["trigger-filter-and-routing"]


def test_legacy_incoming_rows_are_dropped_without_forwarding(tmp_path, caplog):
    service = configured(tmp_path)
    assert not service.enqueue_inbound(user_id=42, source_chat_id=42, message_id=1,
                                       authorized=True, chat_type="dm")
    secret = "GROUP-CONTENT-MUST-NOT-LEAK"
    with _db(service._storage_root) as db:
        for index, (chat_type, source, thread) in enumerate([
            ("group", -81, None), ("supergroup", -1000000000081, None),
            ("group", -1000000000081, "1"), ("group", -1000000000081, "77"),
            ("channel", -1000000000081, None), ("unknown", 42, None),
            ("dm", -81, None), ("dm", 42, "bad"), ("dm", 42, None),
        ], start=10):
            db.execute("""INSERT INTO outbox(dedupe_key,kind,user_id,source_chat_id,
                source_message_id,chat_type,thread_id,payload,next_attempt,created_at)
                VALUES(?,'incoming',42,?,?,?,?,?,?,?)""",
                       (f"legacy:{index}", source, index, chat_type, thread, secret,
                        0, time.time()))
            db.execute("INSERT INTO receipts VALUES(?,?)", (f"legacy:{index}", time.time()))
    # A fresh service represents a restart. Non-private rows are purged when
    # the database opens; the remaining private row is dropped by drain.
    # Nothing reaches any transport callback.
    restored = configured(tmp_path)

    async def forbidden(*args):
        pytest.fail("legacy inbound rows must not reach any transport callback")

    assert asyncio.run(restored.drain(send_text=forbidden)) == 1
    with _db(restored._storage_root) as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM user_topics").fetchone()[0] == 0
    assert secret not in caplog.text
    assert "discarded non-private or unverified queued transcripts" in caplog.text
    assert "dropped a queued inbound message row" in caplog.text
    # Direct persistence callers cannot bypass the public enqueue guard.
    assert not _insert(restored._storage_root, key="bypass", kind="incoming",
                       user_id=42, source_chat_id=-81, source_message_id=99,
                       chat_type="group", payload=secret)


def test_drain_never_resolves_peers_or_creates_topics_for_inbound_rows(tmp_path):
    adapter = _FakeAdapter(tmp_path)
    controller = ForumAuditTransport(adapter, tmp_path)
    controller._service = configured(tmp_path)
    with _db(controller._service._storage_root) as db:
        db.execute("""INSERT INTO outbox(dedupe_key,kind,user_id,source_chat_id,
            source_message_id,chat_type,thread_id,payload,next_attempt,created_at)
            VALUES('legacy:1','incoming',42,42,9,'dm',NULL,'',0,0)""")
    assert asyncio.run(controller._drain_once()) == 1
    assert controller._service.user_topic(42) is None
    with _db(controller._service._storage_root) as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
