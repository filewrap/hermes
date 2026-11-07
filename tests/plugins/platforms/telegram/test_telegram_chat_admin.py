from datetime import datetime, timezone, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.mark.asyncio
async def test_list_chat_messages_is_paged_and_carries_topic_ids():
    message = SimpleNamespace(
        id=91,
        sender_id=7,
        date=datetime(2026, 10, 17, tzinfo=UTC),
        message="topic reply",
        pinned=False,
        out=False,
        reply_to=SimpleNamespace(reply_to_top_id=80),
        action=None,
    )
    client = SimpleNamespace(get_messages=AsyncMock(return_value=[message]))
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = client

    rows = await adapter.list_chat_messages(
        "-10042", limit=100, offset_id=500, query="topic reply", thread_id="80",
    )

    client.get_messages.assert_awaited_once_with("-10042", limit=50, offset_id=500, reply_to=80)
    assert rows == {"messages": [{
        "message_id": "91",
        "thread_id": "80",
        "sender_id": "7",
        "date": "2026-10-17T00:00:00+00:00",
        "text": "topic reply",
        "pinned": False,
        "outgoing": False,
    }], "next_offset_id": "91", "scanned_count": 1}


@pytest.mark.asyncio
async def test_list_pinned_messages_uses_native_telethon_filter():
    from telethon import types

    client = SimpleNamespace(get_messages=AsyncMock(return_value=[]))
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = client

    assert await adapter.list_chat_messages("42", pinned_only=True) == {
        "messages": [], "next_offset_id": None, "scanned_count": 0,
    }
    kwargs = client.get_messages.await_args.kwargs
    assert isinstance(kwargs["filter"], types.InputMessagesFilterPinned)


@pytest.mark.asyncio
async def test_whole_chat_search_uses_native_query_and_pinned_filter():
    from telethon import types

    client = SimpleNamespace(get_messages=AsyncMock(return_value=[]))
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = client

    page = await adapter.list_chat_messages("42", query="equation", pinned_only=True)

    client.get_messages.assert_awaited_once_with(
        "42", limit=20, offset_id=0, search="equation",
        filter=types.InputMessagesFilterPinned(),
    )
    assert page == {"messages": [], "next_offset_id": None, "scanned_count": 0}


@pytest.mark.asyncio
async def test_topic_search_and_pinned_filters_are_local_and_cursor_survives_no_match():
    messages = [
        SimpleNamespace(id=91, sender_id=7, date=None, message="equation is pinned", pinned=True,
                        out=False, reply_to=SimpleNamespace(reply_to_top_id=80), action=None),
        SimpleNamespace(id=90, sender_id=8, date=None, message="equation in another topic", pinned=True,
                        out=False, reply_to=SimpleNamespace(reply_to_top_id=81), action=None),
        SimpleNamespace(id=89, sender_id=9, date=None, message="equation but not pinned", pinned=False,
                        out=False, reply_to=SimpleNamespace(reply_to_top_id=80), action=None),
    ]
    client = SimpleNamespace(get_messages=AsyncMock(return_value=messages))
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = client

    page = await adapter.list_chat_messages(
        "-10042", limit=3, query="equation", pinned_only=True, thread_id=80,
    )

    client.get_messages.assert_awaited_once_with("-10042", limit=3, offset_id=0, reply_to=80)
    assert [row["message_id"] for row in page["messages"]] == ["91"]
    assert page["next_offset_id"] == "89"
    assert page["scanned_count"] == 3


@pytest.mark.asyncio
async def test_pin_and_unpin_validate_target_then_send_mtproto_request():
    class Client:
        def __init__(self):
            self.requests = []
            self.get_messages = AsyncMock(return_value=SimpleNamespace(id=91))

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace()

    client = Client()
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = client

    assert await adapter.pin_chat_message("-10042", "91") == {"message_id": "91", "pinned": True}
    assert await adapter.pin_chat_message("-10042", 91, unpin=True) == {"message_id": "91", "pinned": False}
    assert len(client.requests) == 2
    pin_request, unpin_request = client.requests
    assert type(pin_request).__name__ == "UpdatePinnedMessageRequest"
    assert pin_request.id == 91 and pin_request.silent is True and pin_request.unpin is False
    assert unpin_request.id == 91 and unpin_request.unpin is True


@pytest.mark.asyncio
async def test_pin_refuses_message_not_found_in_current_chat():
    client = SimpleNamespace(get_messages=AsyncMock(return_value=None))
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = client

    with pytest.raises(ValueError, match="not found in the current Telegram chat"):
        await adapter.pin_chat_message("-10042", "91")


@pytest.mark.asyncio
async def test_deep_research_topic_followup_uses_the_same_thread_session_key():
    from gateway.platforms.event import MessageEvent

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    research_source = adapter.build_source(
        "-10042", chat_type="group", user_id="123", user_name="Researcher", thread_id="777",
    )
    research_event = MessageEvent(text="Conduct deep research", source=research_source, user_id="123")
    followup_update = SimpleNamespace(
        message=SimpleNamespace(
            chat_id=-10042,
            id=901,
            reply_to=SimpleNamespace(reply_to_top_id=777, forum_topic=True),
            action=None,
        ),
        chat_id=-10042,
        sender_id=123,
        sender=SimpleNamespace(id=123, first_name="Researcher", last_name=None,
                               username="researcher", bot=False),
        chat=SimpleNamespace(title="Research", megagroup=True, broadcast=False, first_name=None),
    )

    followup_source = await adapter._source(followup_update)
    followup_event = MessageEvent(text="Can you clarify result 2?", source=followup_source, user_id="123")

    assert followup_source.thread_id == "777"
    assert adapter._event_session_key(followup_event) == adapter._event_session_key(research_event)
