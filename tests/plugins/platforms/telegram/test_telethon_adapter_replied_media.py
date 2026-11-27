"""Replied-to media on inbound Telegram messages.

A reply to a photo, sticker or file hands the agent the file itself (port of
upstream _cache_replied_media), not just the usually-empty caption.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _ReplyTriggerMessage:
    """A text message replying to another message."""

    def __init__(self, text, reply, *, photo=None):
        self.message = text
        self.id = 12
        self.reply_to_msg_id = 10
        self.photo = photo
        self.video = self.voice = self.audio = self.sticker = self.document = None
        self._reply = reply

    async def get_reply_message(self):
        return self._reply


class _RepliedPhoto:
    """A replied-to photo message with a caption."""

    message = "look at this"
    sender_id = 7
    photo = object()
    video = voice = audio = sticker = document = None

    async def get_sender(self):
        return SimpleNamespace(username="alice", first_name="Alice")


class _RepliedSticker:
    """A replied-to sticker (a document in Telethon) with no caption."""

    message = ""
    sender_id = 7
    photo = video = voice = audio = None
    sticker = object()
    document = object()

    async def get_sender(self):
        return SimpleNamespace(username="alice", first_name="Alice")


def _cache_for(reply, reply_result, trigger_result=None):
    """A _cache_inbound_media stand-in answering per message identity.

    Returns fresh lists per call, like the real cacher: the adapter extends
    the returned trigger paths in place, so a shared list would leak one
    test's media into the next.
    """
    if trigger_result is None:
        trigger_result = ([], [], "")

    async def fake_cache(message):
        result = reply_result if message is reply else trigger_result
        return (list(result[0]), list(result[1]), result[2])

    return fake_cache


async def _drive_reply(monkeypatch, trigger, cache, *, intake=None):
    """Drive one replying message through the adapter; return the dispatched event."""
    adapter = TelethonAdapter(PlatformConfig())
    adapter._bot_id = 42
    adapter._bot_username = "NainaAssistantBot"
    source = SimpleNamespace(
        user_id="7", user_name="alice", chat_type="group", chat_id="-1001", thread_id=None,
    )
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _: True)
    monkeypatch.setattr(adapter, "_group_triggered", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_cache_inbound_media", cache)
    if intake is not None:
        # The real intake needs a session store; the merge under test only
        # reads its cached paths/types, so stand in a prebuilt intake.
        monkeypatch.setattr(adapter, "_prepare_attachment_intake", AsyncMock(return_value=intake))
    adapter.handle_message = AsyncMock()
    await adapter._on_new_message(SimpleNamespace(message=trigger))
    assert adapter.handle_message.await_count == 1
    return adapter.handle_message.await_args.args[0]


@pytest.mark.asyncio
async def test_inbound_reply_to_photo_attaches_replied_media_to_event(monkeypatch):
    reply = _RepliedPhoto()
    trigger = _ReplyTriggerMessage("what is this?", reply)
    event = await _drive_reply(
        monkeypatch, trigger,
        _cache_for(reply, (["/cache/replied.jpg"], ["image/jpeg"], "")),
    )
    assert event.media_urls == ["/cache/replied.jpg"]
    assert event.media_types == ["image/jpeg"]
    assert event.reply_to_message_id == "10"
    assert event.reply_to_text == "look at this"


@pytest.mark.asyncio
async def test_inbound_reply_to_sticker_attaches_replied_media_to_event(monkeypatch):
    reply = _RepliedSticker()
    trigger = _ReplyTriggerMessage("what is this?", reply)
    event = await _drive_reply(
        monkeypatch, trigger,
        _cache_for(reply, (["/cache/replied.webp"], ["image/webp"], "")),
    )
    assert event.media_urls == ["/cache/replied.webp"]
    assert event.media_types == ["image/webp"]
    assert event.reply_to_message_id == "10"


@pytest.mark.asyncio
async def test_inbound_reply_to_text_message_attaches_no_media(monkeypatch):
    reply = _RepliedPhoto()
    reply.photo = None
    trigger = _ReplyTriggerMessage("what is this?", reply)
    event = await _drive_reply(monkeypatch, trigger, _cache_for(reply, ([], [], "")))
    assert event.media_urls == []
    assert event.media_types == []
    assert event.reply_to_text == "look at this"


@pytest.mark.asyncio
async def test_inbound_reply_to_oversize_media_is_noted_not_attached(monkeypatch):
    reply = _RepliedPhoto()
    trigger = _ReplyTriggerMessage("what is this?", reply)
    event = await _drive_reply(
        monkeypatch, trigger,
        _cache_for(reply, ([], [], "[Telegram attachment omitted: it exceeds the configured size limit]")),
    )
    assert event.media_urls == []
    assert "[Telegram attachment omitted: it exceeds the configured size limit]" in event.text


@pytest.mark.asyncio
async def test_inbound_reply_keeps_triggering_message_media_too(monkeypatch):
    reply = _RepliedPhoto()
    trigger = _ReplyTriggerMessage("what is this?", reply, photo=object())
    intake = SimpleNamespace(
        _cached_paths=["/cache/trigger.jpg"], _cached_media_types=["image/jpeg"], note="",
    )
    event = await _drive_reply(
        monkeypatch, trigger,
        _cache_for(reply, (["/cache/replied.jpg"], ["image/jpeg"], ""),
                   trigger_result=(["/cache/trigger.jpg"], ["image/jpeg"], "")),
        intake=intake,
    )
    assert event.media_urls == ["/cache/trigger.jpg", "/cache/replied.jpg"]
    assert event.media_types == ["image/jpeg", "image/jpeg"]


@pytest.mark.asyncio
async def test_inbound_unfetchable_reply_falls_back_to_text_only(monkeypatch):
    class _UnfetchableTrigger(_ReplyTriggerMessage):
        async def get_reply_message(self):
            raise RuntimeError("message gone")

    trigger = _UnfetchableTrigger("what is this?", None)
    event = await _drive_reply(monkeypatch, trigger, _cache_for(None, ([], [], "")))
    assert event.media_urls == []
    assert event.reply_to_text is None
    assert event.reply_to_message_id == "10"
