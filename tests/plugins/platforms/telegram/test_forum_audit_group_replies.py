"""Installed forum-audit hooks must never intercept normal authorized group turns."""
from __future__ import annotations

from datetime import datetime, timezone, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.forum_audit import _db
from plugins.platforms.telegram.forum_audit_transport import install
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


_FORUM_ID = -1000000000123


def _telegram_event(scope: str, trigger: str):
    """Build a real Telethon message and peer/entity graph, without a client."""
    now = datetime.now(UTC)
    sender = types.User(id=42, access_hash=4200, first_name="Alice")
    if scope == "basic group":
        chat = types.Chat(id=81, title="Basic group", photo=types.ChatPhotoEmpty(),
                          participants_count=3, date=now, version=1)
        peer = types.PeerChat(81)
    else:
        forum = scope.startswith("forum ")
        chat = types.Channel(id=81, title=scope, photo=types.ChatPhotoEmpty(), date=now,
                             megagroup=True, forum=forum, access_hash=8100)
        peer = types.PeerChannel(81)

    if trigger == "mention":
        text = "@hermes could you help with this?"
    elif trigger == "reply":
        text = "Following up on your answer"
    else:
        text = "An ordinary group message"

    reply_to = None
    if trigger == "reply":
        # A named-topic reply carries a topic anchor; ordinary group/forum replies
        # are deliberately kept distinct from a forum General thread.
        reply_to = types.MessageReplyHeader(
            reply_to_msg_id=800,
            reply_to_top_id=77 if scope == "forum named topic" else None,
            forum_topic=scope == "forum named topic",
        )
    elif scope == "forum named topic":
        # An ordinary post in a topic has a header pointing at that topic root.
        reply_to = types.MessageReplyHeader(
            reply_to_msg_id=77, reply_to_top_id=77, forum_topic=True,
        )

    message = types.Message(
        id=101, peer_id=peer, from_id=types.PeerUser(42), date=now,
        message=text, reply_to=reply_to,
    )
    if trigger == "reply":
        async def get_reply_message():
            return types.Message(
                id=800, peer_id=peer, from_id=types.PeerUser(999), date=now,
                message="A prior bot response",
            )
        # This is the local equivalent of Telethon's reply lookup; it avoids a
        # network/client call while retaining the real Message's reply behavior.
        message.get_reply_message = get_reply_message

    event = SimpleNamespace(
        message=message, chat=chat, sender=sender, chat_id=message.chat_id,
        sender_id=sender.id,
    )
    return event, peer


class _UI:
    async def handle_message(self, *args, **kwargs):
        return False


@pytest.mark.parametrize("scope", [
    "basic group", "supergroup", "forum General", "forum named topic",
])
@pytest.mark.parametrize("trigger", ["ordinary", "mention", "reply"])
@pytest.mark.asyncio
async def test_authorized_group_turn_reaches_normal_handler_but_never_audit_outbox(
        tmp_path, monkeypatch, scope, trigger):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig(extra={
        # Exercise ordinary posts, textual mentions, and actual replies as three
        # independently eligible trigger paths.
        "require_mention": trigger != "ordinary",
    }))
    adapter._bot_username = "hermes"
    adapter._bot_id = 999
    # The trigger policy requires an active Telegram client before it resolves
    # a reply; the message-local resolver below keeps that lookup offline.
    adapter._client = object()
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(adapter, "_is_explicitly_allowed", lambda _source: True)
    monkeypatch.setattr(adapter, "_settings_home", lambda _source: tmp_path)
    monkeypatch.setattr(adapter, "_multiuser_home", lambda _source: None)
    monkeypatch.setattr(adapter, "_chat_settings", lambda _source: {})
    monkeypatch.setattr(adapter, "_observe_telegram_message", lambda *_args: None)
    monkeypatch.setattr(adapter, "_refresh_command_menu", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_prepare_attachment_intake", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_cache_inbound_peer", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_cache_inbound_media", AsyncMock(return_value=([], [], "")))
    monkeypatch.setattr(adapter, "_setup_ui", lambda: _UI())
    monkeypatch.setattr(adapter, "_model_settings_ui", lambda: _UI())
    monkeypatch.setattr(adapter, "_provider_ui", lambda: _UI())
    monkeypatch.setattr(adapter, "_settings_ui", lambda: _UI())
    monkeypatch.setattr(adapter, "_skills_ui", lambda: _UI())
    monkeypatch.setattr(adapter, "_invoke_control_ui_message", AsyncMock(return_value=False))
    monkeypatch.setattr(adapter, "_handle_telegram_operation_command", AsyncMock(return_value=False))
    monkeypatch.setattr(adapter, "_handle_control_command", AsyncMock(return_value=False))

    from plugins.platforms.telegram.thread_ui import TelegramThreadUI
    monkeypatch.setattr(TelegramThreadUI, "remember_source_topic", lambda *_args: None)
    from plugins.platforms.telegram import incoming_reactions
    monkeypatch.setattr(incoming_reactions, "handle_explicit_reaction_request",
                        AsyncMock(return_value=None))
    monkeypatch.setattr(incoming_reactions, "react_to_telethon_incoming",
                        AsyncMock(return_value=None))
    monkeypatch.setattr(incoming_reactions, "react_to_incoming",
                        AsyncMock(return_value=None))

    controller = install(adapter, tmp_path)
    controller._new_service({
        "enabled": True,
        "chat_id": _FORUM_ID,
        "general_topic_id": 1,
        "operations_topic_id": 2,
        "verified_entity": True,
        "verified_forum": True,
        "verified_rights": True,
    }, runtime_verified=True)
    assert controller._service.enabled  # Exercise the real ForumAuditLogger.

    event, peer = _telegram_event(scope, trigger)
    assert isinstance(event.message.peer_id, (types.PeerChat, types.PeerChannel))
    assert event.message.peer_id == peer
    source = await adapter._source(event)  # Exercise real Telethon source classification.
    assert source.chat_id == ("-81" if scope == "basic group" else "-1000000000081")
    assert source.thread_id == ({"forum General": "1", "forum named topic": "77"}.get(scope))

    handled = []

    async def normal_model_handler(message_event):
        handled.append(message_event)

    monkeypatch.setattr(adapter, "handle_message", normal_model_handler)
    await adapter._process_inbound_event(event, [event.message])

    assert len(handled) == 1, "the installed audit hook must preserve the normal model turn"
    # The mention trigger strips the sole-addressee @bot from the event text
    # (mention-stripping behaviour); ordinary and reply triggers keep it raw.
    expected_text = event.message.message
    if trigger == "mention":
        expected_text = expected_text.removeprefix("@hermes ")
    assert handled[0].text == expected_text
    assert handled[0].source.chat_id == source.chat_id
    assert handled[0].source.thread_id == source.thread_id
    if trigger == "reply":
        assert handled[0].reply_to_is_own_message is True

    # Group posts, even when explicitly authorized and normally triggerable, must
    # not be copied into any audit topic or create per-user topic mappings.
    with _db(controller._service._storage_root) as db:
        assert db.execute("SELECT kind FROM outbox").fetchall() == []
        assert db.execute("SELECT user_id, topic_id FROM user_topics").fetchall() == []
