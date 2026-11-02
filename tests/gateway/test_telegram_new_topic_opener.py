"""Client-created Telegram DM topic: thread-less opener adopts the new topic (#136567).

Telegram for Android 13.0.x sends the opener of a topic created from "All messages" without
``message_thread_id``; only the preceding ``forum_topic_created`` service message names the topic.

Telethon shape: ``NewMessage`` never fires for service messages (``MessageService`` is not a
``Message``), so the creation is recorded from the Raw ``UpdateNewMessage`` handler reading the
``MessageActionTopicCreate`` action, and the opener adopts it in ``_process_inbound_event``
right after ``_source()``. The 5 s follow window is measured on ``message.date`` stamps, never
handler time.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import MessageActionTopicCreate, PeerUser

from gateway.config import PlatformConfig
from plugins.platforms.telegram import telegram_topic_routing as routing
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

CHAT, USER, NEW_TOPIC = 111, 42, 600
T0 = datetime(2026, 10, 14, 13, 21, 2, tzinfo=UTC)
WINDOW = routing.TelegramTopicRoutingMixin._NEW_TOPIC_FOLLOW_SECONDS


def _service_message(message_id, sent, *, user_id=USER, chat_id=CHAT):
    # The creation service message's own id IS the new topic id.
    return SimpleNamespace(
        id=message_id,
        peer_id=PeerUser(chat_id),
        sender_id=user_id,
        date=T0 + timedelta(seconds=sent),
        action=MessageActionTopicCreate(title="New topic", icon_color=0x6FB9F0),
    )


def _msg(message_id, sent, *, thread_id=None, user_id=USER, chat_id=CHAT, text="hi", edited=None):
    # A regular Telethon Message-shaped fake: sender_id + date drive adoption; reply_to
    # mirrors what _source() would read from a message already stamped into a topic.
    reply_to = (SimpleNamespace(reply_to_top_id=thread_id, forum_topic=True)
                if thread_id is not None else None)
    return SimpleNamespace(
        id=message_id,
        sender_id=user_id,
        chat_id=chat_id,
        message=text,
        date=T0 + timedelta(seconds=sent),
        reply_to=reply_to,
        edit_date=None if edited is None else T0 + timedelta(seconds=edited),
    )


@pytest.fixture
def adapter():
    a = TelethonAdapter(PlatformConfig(enabled=True, token="***", extra={"allow_from": [str(USER)]}))
    # The Raw creation handler records only when the registered check says True;
    # user 7 below is the "adapter would reject" sender.
    a.set_authorization_check(lambda user_id, chat_type, chat_id, **kw: user_id == str(USER))
    return a


async def _create(adapter, message_id=NEW_TOPIC, sent=0.0, **kw):
    await adapter._on_topic_created_raw(SimpleNamespace(message=_service_message(message_id, sent, **kw)))


def _thread(adapter, msg, thread_id=None):
    # What _process_inbound_event passes after _source(): the message's own thread id
    # (None for a thread-less opener), resolved against the recorded creation.
    return adapter._adopt_created_topic_thread(msg, msg.chat_id, "dm", thread_id)


@pytest.mark.asyncio
async def test_thread_less_messages_after_creation_land_in_the_new_topic(adapter):
    await _create(adapter)
    record = adapter._pending_created_topics[(str(CHAT), str(USER))]
    assert record[0] == str(NEW_TOPIC)
    assert "New topic" not in repr(record)  # the created name is never cached anywhere
    assert not hasattr(adapter, "_dm_topics")
    # Not creation+1: private-chat ids are one sequence shared by every chat of the bot.
    assert _thread(adapter, _msg(9382, 0.2)) == str(NEW_TOPIC)
    # The next chunk was sent 0.5 s later but its handler ran 30 s later (per-chat FIFO behind a
    # slow photo download / STT): the window follows send time, so it still lands in the topic.
    assert _thread(adapter, _msg(9383, 0.7, text="second chunk")) == str(NEW_TOPIC)
    # A message the client stamped ends the follow; a later lobby message stays lobby.
    assert _thread(adapter, _msg(9384, 1.0, thread_id=NEW_TOPIC), thread_id=str(NEW_TOPIC)) == str(NEW_TOPIC)
    assert _thread(adapter, _msg(9385, 1.2)) is None
    assert adapter._pending_created_topics == {}


@pytest.mark.asyncio
async def test_creation_is_not_adopted_late_or_by_others(adapter):
    await _create(adapter)
    assert _thread(adapter, _msg(11, 0.1, user_id=7)) is None  # another sender
    assert _thread(adapter, _msg(12, 0.1, chat_id=222)) is None  # another chat
    # An edit of a lobby message sent before the creation stays where that message lived.
    assert _thread(adapter, _msg(5, -60, edited=1.0)) is None
    # A lobby message sent well after the creation does not adopt (and consumes the record).
    assert _thread(adapter, _msg(13, WINDOW + 1)) is None
    await _create(adapter, user_id=7)  # creation by a user the adapter would reject
    assert not adapter._pending_created_topics


@pytest.mark.asyncio
async def test_second_opener_within_window_also_adopts(adapter):
    # The mixin re-records on adopt (sliding window): a second opener within 5 s of the
    # first also adopts; the record is consumed only by a stamped message or expiry.
    await _create(adapter)
    assert _thread(adapter, _msg(9382, 0.2)) == str(NEW_TOPIC)
    assert _thread(adapter, _msg(9383, 0.7, text="second chunk")) == str(NEW_TOPIC)
    assert _thread(adapter, _msg(9384, 4.5, text="third chunk")) == str(NEW_TOPIC)
    assert _thread(adapter, _msg(9385, 10.0)) is None  # 5.5 s after the last adopted opener


@pytest.mark.asyncio
async def test_opener_adopts_through_process_inbound_event(adapter):
    await _create(adapter)
    source = adapter.build_source(
        str(CHAT), chat_name="T", chat_type="dm", user_id=str(USER), user_name="T",
        thread_id=None, is_bot=False, message_id="9382",
    )
    msg = _msg(9382, 0.2)
    event = SimpleNamespace(message=msg, chat_id=CHAT, sender_id=USER)
    # _source() is the only stubbed seam: the adoption hook, the funnel gates and the
    # MessageEvent construction all run for real.
    adapter._source = AsyncMock(return_value=source)
    seen = []
    adapter.handle_message = AsyncMock(side_effect=lambda event_obj: seen.append(event_obj))
    await adapter._process_inbound_event(event, [msg])
    assert len(seen) == 1
    assert seen[0].source.thread_id == str(NEW_TOPIC)
    assert seen[0].metadata == {"thread_id": str(NEW_TOPIC)}
