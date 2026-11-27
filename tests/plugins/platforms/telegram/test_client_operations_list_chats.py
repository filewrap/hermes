"""list_chats lists the bot account's dialogs via Telethon get_dialogs."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from plugins.platforms.telegram.client_operations import TelegramClientOperations


def _dialog(dialog_id, name, *, user=False, group=False, channel=False, broadcast=False,
            username=None, preview="last words", unread=0):
    # The projection reads only username/broadcast off the entity; the id
    # comes from the dialog itself (Telethon's marked peer id).
    entity = SimpleNamespace(username=username, broadcast=broadcast)
    return SimpleNamespace(
        id=dialog_id, name=name, entity=entity,
        is_user=user, is_group=group or (channel and not broadcast), is_channel=channel,
        message=SimpleNamespace(message=preview),
        unread_count=unread, date=datetime(2026, 11, 11, tzinfo=UTC),
    )


@pytest.fixture
def ops(tmp_path):
    client = SimpleNamespace(get_dialogs=AsyncMock(return_value=[
        _dialog(100, "Ann", user=True, username="ann", unread=2),
        _dialog(-10042, "Testing Agents", group=True, preview="hello group"),
        _dialog(-10077, "Old Library", channel=True, broadcast=True, preview="song"),
    ]))
    adapter = SimpleNamespace(
        _client=client,
        _authorized=lambda _source: True,
        _is_owner_user=lambda _user, source=None, **_kw: True,
        _settings_home=lambda _source: tmp_path / "alpha",
        _input_peer_for=AsyncMock(return_value="resolved-peer"),
        _mtproto_rich_request=AsyncMock(return_value=SimpleNamespace(updates=[])),
        _sent_message_id=lambda *args, **kwargs: 555,
        _message_thread_id=lambda _message: None,
    )
    return TelegramClientOperations(adapter), client


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="-10042", chat_type="group",
                         user_id="100", thread_id="77")


@pytest.mark.asyncio
async def test_list_chats_projects_dialogs(ops):
    operations, _client = ops
    result = await operations.request(_source(), "list_chats", {"limit": 20})
    assert result["ok"] is True
    assert result["status"] == "complete"
    chats = result["data"]["chats"]
    assert result["data"]["total"] == 3
    assert chats[0] == {
        "id": "100", "name": "Ann", "username": "ann", "type": "dm",
        "unread_count": 2, "last_message": "last words",
        "last_message_date": "2026-11-11T00:00:00+00:00",
    }
    assert chats[1]["type"] == "group" and chats[1]["id"] == "-10042"
    assert chats[2]["type"] == "channel" and chats[2]["id"] == "-10077"


@pytest.mark.asyncio
async def test_list_chats_passes_limit_to_get_dialogs(ops):
    operations, client = ops
    await operations.request(_source(), "list_chats", {"limit": 5})
    client.get_dialogs.assert_awaited_once_with(limit=5)


@pytest.mark.asyncio
async def test_list_chats_rejects_out_of_range_limit(ops):
    operations, _client = ops
    result = await operations.request(_source(), "list_chats", {"limit": 500})
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_list_chats_is_listed_as_a_read_in_capabilities(ops):
    operations, _client = ops
    result = await operations.request(_source(), "capabilities", {})
    assert "list_chats" in result["data"]["reads"]
    assert "limit" in result["data"]["parameters"]["list_chats"]
