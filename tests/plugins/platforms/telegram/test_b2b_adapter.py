"""Behaviour contracts for the B2B adapter seam: admission runs before the
human group-trigger gate, admitted peers reach the turn path, and peer text
never becomes a human command or UI interaction.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.b2b_handshake import (
    arm_talk_closed_note,
    live_talk_record,
    talk_session_key,
)
from plugins.platforms.telegram.bot_operations import BotOperationsStore
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

OWNER = "100"
PEER = "42"
GROUP = "-100123"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text("OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100\n")
    return home


def _source(user_id=OWNER, *, chat_type="group", chat_id=GROUP, is_bot=False):
    return SimpleNamespace(
        platform="telegram",
        chat_id=str(chat_id),
        chat_type=chat_type,
        user_id=str(user_id),
        user_name=f"User {user_id}",
        chat_name=f"Chat {chat_id}",
        thread_id=None,
        is_bot=is_bot,
        profile=None,
    )


def _message(text, source, *, message_id=55):
    return SimpleNamespace(
        source=source,
        message=SimpleNamespace(
            message=text, id=message_id, reply_to_msg_id=None,
            get_reply_message=AsyncMock(return_value=None),
        ),
        get_sender=AsyncMock(return_value=SimpleNamespace(
            id=int(source.user_id),
            username=f"user_{source.user_id}",
            first_name=f"User {source.user_id}",
            bot=source.is_bot,
        )),
    )


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = _home(tmp_path)
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": True}))
    adapter._bot_username = "NainaAssistantBot"
    adapter._bot_name = "Naina"
    adapter._bot_id = 999
    adapter.send_reaction = AsyncMock(return_value=True)
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda src: home,
        async_session_store=SimpleNamespace(set_model_override=AsyncMock()),
    )
    adapter._source = AsyncMock(side_effect=lambda event: event.source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter._send_rich_ui = AsyncMock(return_value=SimpleNamespace(id=55))
    adapter._edit_rich_ui_message = AsyncMock()
    adapter.send = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._input_peer_for = AsyncMock(
        side_effect=lambda uid: types.InputPeerUser(int(uid), 98765))
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=901))
    adapter._client.get_entity = AsyncMock()
    adapter._client.get_input_entity = AsyncMock(
        side_effect=lambda peer: types.InputUser(int(getattr(peer, "user_id", peer)), 98765))
    adapter._peer_for = lambda chat_id: types.InputPeerSelf()
    adapter._session_key_profile = lambda _source: None
    monkeypatch.setattr(
        "plugins.platforms.telegram.incoming_reactions.react_to_incoming", AsyncMock())
    store = BotOperationsStore(home)
    return SimpleNamespace(adapter=adapter, home=home, store=store)


def _add_peer(store, *, group=True, **settings):
    store.add_peer({"id": int(PEER), "username": "peer42_bot"},
                   group_enabled=group, dm_enabled=True, group_chat_id=GROUP)
    if settings:
        store.set_peer_settings(int(PEER), settings)


async def _send(transport, text, source):
    await transport.adapter._on_new_message(_message(text, source))


@pytest.mark.asyncio
async def test_admitted_peer_in_a_group_reaches_a_turn_without_addressing(transport):
    _add_peer(transport.store, trigger="direct", notify="off")
    await _send(transport, "hello", _source(PEER, is_bot=True))
    transport.adapter.handle_message.assert_awaited_once()
    event = transport.adapter.handle_message.await_args.args[0]
    assert event.source.b2b_session_key == talk_session_key(None, int(PEER))
    assert event.allow_gateway_control is False
    assert event._b2b_admitted is True


@pytest.mark.asyncio
async def test_unadmitted_bot_in_a_group_is_dropped_silently(transport):
    await _send(transport, "hello", _source(PEER, is_bot=True))
    transport.adapter.handle_message.assert_not_awaited()
    transport.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_group_message_without_a_trigger_is_still_dropped(transport):
    await _send(transport, "hello", _source("201"))
    transport.adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_peer_turn_carries_the_closed_note_once(transport):
    _add_peer(transport.store, trigger="direct", notify="off")
    arm_talk_closed_note(transport.home, talk_session_key(None, int(PEER)), int(PEER))
    await _send(transport, "hello again", _source(PEER, is_bot=True))
    transport.adapter.handle_message.assert_awaited_once()
    event = transport.adapter.handle_message.await_args.args[0]
    assert "stopped by the owner" in event.text
    assert "hello again" in event.text
    await _send(transport, "and again", _source(PEER, is_bot=True))
    second = transport.adapter.handle_message.await_args.args[0]
    assert "stopped by the owner" not in second.text


@pytest.mark.asyncio
async def test_peer_slash_text_is_model_input_not_a_command(transport):
    _add_peer(transport.store, trigger="direct", notify="off")
    await _send(transport, "/stop", _source(PEER, is_bot=True))
    transport.adapter.handle_message.assert_awaited_once()
    event = transport.adapter.handle_message.await_args.args[0]
    assert event.get_command() is None
    assert event.text == "/stop"


@pytest.mark.asyncio
async def test_owner_bstop_in_dm_kills_the_live_talk(transport):
    _add_peer(transport.store, notify="off")
    transport.store.upsert_talk(int(PEER), live_talk_record(int(PEER)))
    await _send(transport, "/bstop", _source(OWNER, chat_type="dm", chat_id=OWNER))
    assert transport.store.get_talk(int(PEER))["state"] == "killed"
    sent = [call.args for call in transport.adapter.send.await_args_list]
    assert ("42", "3083 42") in [(args[0], args[1]) for args in sent]
    transport.adapter.handle_message.assert_not_awaited()
