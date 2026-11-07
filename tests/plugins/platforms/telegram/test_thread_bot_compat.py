"""Bots cannot enumerate topics, but can refresh scoped, known IDs natively."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

from plugins.platforms.telegram.thread_ui import TelegramThreadUI


@pytest.mark.asyncio
async def test_bot_list_and_actions_never_enumerate_and_refresh_live_metadata(tmp_path, monkeypatch):
    calls = []
    deleted = set()
    closed = set()
    # Threaded bot DMs use user peers, not channel peers, in the pinned MTProto layer.
    peer = types.InputPeerUser(user_id=123, access_hash=456)

    async def rpc(request):
        calls.append(request)
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            raise errors.BotMethodInvalidError(request)
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            return SimpleNamespace(topics=[SimpleNamespace(id=i) if i in deleted else SimpleNamespace(
                id=i, title=f"Live title {i}", top_message=i + 100,
                closed=i in closed, hidden=False,
            ) for i in reversed(request.topics)])
        if isinstance(request, functions.messages.EditForumTopicRequest):
            closed.add(request.topic_id)
        if isinstance(request, functions.messages.DeleteTopicHistoryRequest):
            deleted.add(request.top_msg_id)
        return SimpleNamespace()

    source = SimpleNamespace(chat_id="123", user_id="111", chat_type="dm", thread_id="2", is_bot=False)
    adapter = SimpleNamespace(
        _client=rpc, _bot_id=999, _peer_for=lambda chat: peer,
        _is_owner_user=lambda uid, source: uid == "111", _authorized=lambda source: True,
        _settings_home=lambda source: tmp_path, _send_rich_ui=AsyncMock(return_value=SimpleNamespace(id=901)),
        _edit_rich_ui_message=AsyncMock(),
    )
    ui = TelegramThreadUI(adapter)
    monkeypatch.setattr(ui, "_profile_scope", lambda home: nullcontext())
    for topic_id in range(2, 12):
        ui.remember_source_topic(source, topic_id)

    await ui.handle_command(source, "list 2")
    text = adapter._send_rich_ui.await_args.args[1]
    assert "Live title 10" in text and "Live title 11" in text
    assert "Page 2 of 2" in text and "known" in text.lower()
    assert "Live title 2<" not in text
    assert all(not isinstance(call, functions.messages.GetForumTopicsRequest) for call in calls)
    assert all(call.peer is peer for call in calls)
    assert all(bytes(call) for call in calls)

    await ui.handle_command(source, "close 2")
    assert 2 in closed
    assert "Closed" in adapter._send_rich_ui.await_args.args[1]
    # Deleted/unknown IDs are not counted or offered for actions after refresh.
    deleted.update({10, 11})
    await ui.handle_command(source, "list 2")
    text = adapter._send_rich_ui.await_args.args[1]
    assert "total 8" in text and "Page 1 of 1" in text
    assert "Live title 10" not in text


@pytest.mark.asyncio
async def test_empty_known_topics_does_not_probe_ids_or_claim_complete_list(tmp_path, monkeypatch):
    rpc = AsyncMock(side_effect=AssertionError("No known IDs: do not probe Telegram"))
    source = SimpleNamespace(chat_id="123", user_id="111", chat_type="dm", thread_id=None, is_bot=False)
    adapter = SimpleNamespace(
        _client=rpc, _bot_id=999, _peer_for=lambda chat: f"peer:{chat}",
        _is_owner_user=lambda uid, source: True, _authorized=lambda source: True,
        _settings_home=lambda source: tmp_path, _send_rich_ui=AsyncMock(return_value=SimpleNamespace(id=901)),
    )
    ui = TelegramThreadUI(adapter)
    monkeypatch.setattr(ui, "_profile_scope", lambda home: nullcontext())
    await ui.handle_command(source, "list")
    text = adapter._send_rich_ui.await_args.args[1]
    assert "known" in text.lower() and "send a message" in text.lower()
    rpc.assert_not_awaited()

    # Knowing an ID is not permission to read or mutate it.
    ui.remember_source_topic(source, 3)
    adapter._authorized = lambda target: target.thread_id != "3"
    await ui.handle_command(source, "list")
    await ui.handle_command(source, "close 3")
    rpc.assert_not_awaited()
