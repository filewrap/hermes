from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.group_membership import (
    TelegramGroupMembershipNotifier,
    _transition,
)
from plugins.platforms.telegram.telegram_chat_settings import load_chat_settings
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class ChatParticipant:
    pass


class ChatParticipantLeft:
    pass


class ChatParticipantBanned:
    pass


def test_transition_recognizes_only_bot_add_removal_and_ban():
    update = SimpleNamespace(user_id=42, prev_participant=None, new_participant=ChatParticipant())
    assert _transition(update, 42) == "added"
    update.prev_participant, update.new_participant = ChatParticipant(), ChatParticipantLeft()
    assert _transition(update, 42) == "removed"
    update.new_participant = ChatParticipantBanned()
    assert _transition(update, 42) == "banned"
    assert _transition(update, 99) is None
    update.prev_participant, update.new_participant = ChatParticipant(), ChatParticipant()
    assert _transition(update, 42) is None


@pytest.mark.asyncio
async def test_membership_event_never_invents_an_audit_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig())
    adapter._bot_id = 42
    adapter._client = AsyncMock()
    adapter._mtproto_rich_request = AsyncMock()
    notifier = TelegramGroupMembershipNotifier(adapter)
    update = SimpleNamespace(
        user_id=42, actor_id=100, prev_participant=None,
        new_participant=ChatParticipant(), channel_id=77,
    )

    await notifier.handle_update(update)

    adapter._client.get_entity.assert_not_awaited()
    adapter._mtproto_rich_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_block_button_requires_owner_confirmation_and_blocks_only_the_notified_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    adapter = TelethonAdapter(PlatformConfig())
    adapter._is_owner_user = lambda user_id, source=None: str(user_id) == "100"
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: tmp_path)
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerChannel(11, 123))
    adapter._mtproto_rich_request = AsyncMock(return_value=SimpleNamespace(id=88))
    service = SimpleNamespace(enabled=True, forum_id=-10011, operations_topic_id=77)
    controller = SimpleNamespace(home=Path(tmp_path).resolve(), _service=service, _revoked=False)
    notifier = TelegramGroupMembershipNotifier(adapter)
    notifier._controller = lambda: controller
    token = "one-shot-token"
    notifier.actions[token] = {
        "created": time.monotonic(), "home": str(Path(tmp_path).resolve()),
        "chat_id": "-10044", "chat_kind": "group", "actor_id": "200",
        "action": "block", "message_id": "55", "audit_chat_id": "-10011",
        "topic_id": "77",
    }
    owner_source = SimpleNamespace(user_id="100", chat_id="-10011", thread_id="77")
    event = SimpleNamespace(message_id=55, answer=AsyncMock())

    await notifier.handle_callback(event, owner_source, f"gma:{token}")
    assert load_chat_settings(tmp_path, "group", "-10044")["blocked"] is False
    review_request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(review_request, functions.messages.EditMessageRequest)
    assert "Confirm chat block" in review_request.rich_message.html

    stranger_source = SimpleNamespace(user_id="101", chat_id="-10011", thread_id="77")
    await notifier.handle_callback(event, stranger_source, f"gma:{token}:confirm")
    assert load_chat_settings(tmp_path, "group", "-10044")["blocked"] is False

    await notifier.handle_callback(event, owner_source, f"gma:{token}:confirm")
    assert load_chat_settings(tmp_path, "group", "-10044")["blocked"] is True
    assert load_chat_settings(tmp_path, "group", "-10045")["blocked"] is False
    assert token not in notifier.actions
