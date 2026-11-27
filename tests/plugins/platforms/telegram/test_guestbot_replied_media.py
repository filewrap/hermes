"""Guest chat reads replied media: a guest question replying to a photo
must carry the image into the turn (real TL messages, real JPEG bytes,
real cache + classification — only the MTProto download itself is stubbed,
since tests never reach the network).

Before this fix the guest handler built its prompt from text excerpts only:
the replied photo never reached the turn, so the guest answer could not
see it. The non-guest path already caches replied media via
``_cache_inbound_media`` / ``_resolve_reply_context``; guest updates carry
raw TL messages (no ``.photo`` / ``.document`` / ``.file``), so the guest
path needs the TL-aware variant.
"""

import asyncio
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from gateway.config import PlatformConfig
from gateway.run_inbound import GatewayInboundMixin
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _jpeg_bytes(*, color="red", size=(64, 48)):
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _png_bytes(*, color="blue", size=(32, 32)):
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _photo_message(*, message_id, photo_id=111, sizes=(1024, 12345), text="look at this photo"):
    photo = types.Photo(
        id=photo_id, access_hash=222, file_reference=b"ref",
        date=datetime.now(timezone.utc),
        sizes=[
            types.PhotoSize(type="s", w=64, h=48, size=sizes[0]),
            types.PhotoSize(type="m", w=320, h=240, size=sizes[1]),
        ],
        dc_id=2,
    )
    return types.Message(
        id=message_id, peer_id=types.PeerChat(chat_id=88),
        date=datetime.now(timezone.utc), message=text,
        media=types.MessageMediaPhoto(photo=photo),
        from_id=types.PeerUser(user_id=77),
    )


def _document_message(*, message_id, filename="diagram.png", mime="image/png", size=4321):
    document = types.Document(
        id=333, access_hash=444, file_reference=b"ref",
        date=datetime.now(timezone.utc), mime_type=mime, size=size,
        attributes=[types.DocumentAttributeFilename(file_name=filename)],
        dc_id=2,
    )
    return types.Message(
        id=message_id, peer_id=types.PeerChat(chat_id=88),
        date=datetime.now(timezone.utc), message="",
        media=types.MessageMediaDocument(document=document),
        from_id=types.PeerUser(user_id=77),
    )


def _query_message(*, message_id=500, reply_to_msg_id=None, text="what is in this photo?", media=None):
    return types.Message(
        id=message_id, peer_id=types.PeerChat(chat_id=88),
        date=datetime.now(timezone.utc), message=text, media=media,
        from_id=types.PeerUser(user_id=77),
        reply_to=(
            types.MessageReplyHeader(reply_to_msg_id=reply_to_msg_id)
            if reply_to_msg_id is not None else None
        ),
    )


class _GuestClient:
    """Fake Telethon client: records raw requests, sent messages, deletes,
    and serves real image bytes for ``download_media`` (the MTProto
    transport is the one seam tests cannot drive)."""

    def __init__(self, *, media_bytes=None):
        self.requests = []
        self.sent_messages = []
        self.deleted = []
        self._next_message_id = 900
        self.media_bytes = media_bytes if media_bytes is not None else _jpeg_bytes()
        self.downloaded = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace()

    async def send_message(self, peer, text, *, reply_to=None):
        self._next_message_id += 1
        record = {"peer": peer, "text": text, "reply_to": reply_to, "id": self._next_message_id}
        self.sent_messages.append(record)
        return SimpleNamespace(id=self._next_message_id)

    async def delete_messages(self, peer, message_ids):
        self.deleted.append({"peer": peer, "message_ids": list(message_ids)})

    async def download_media(self, message, file=bytes):
        self.downloaded.append(message)
        return bytes(self.media_bytes)


def _guest_adapter(client, handler, *, authorized=True, extra=None):
    adapter = TelethonAdapter(PlatformConfig(extra=extra or {}))
    adapter._authorized = lambda _source: authorized
    adapter._client = client
    adapter._message_handler = handler
    return adapter


def _guest_update(message, *, reference_messages=None, query_id=1234):
    return SimpleNamespace(
        query_id=query_id, message=message,
        reference_messages=list(reference_messages or []),
    )


@pytest.mark.asyncio
async def test_guest_reply_to_photo_carries_the_image_into_the_turn():
    jpeg = _jpeg_bytes()
    client = _GuestClient(media_bytes=jpeg)
    seen = []

    async def handle(event):
        seen.append(event)
        return "I see a red photo"

    adapter = _guest_adapter(client, handle)
    replied = _photo_message(message_id=499)
    query = _query_message(reply_to_msg_id=499)
    await adapter._on_guest_chat_query(_guest_update(query, reference_messages=[replied]))

    assert len(seen) == 1
    event = seen[0]
    assert len(event.media_urls) == 1
    assert event.media_types == ["image/jpeg"]
    cached = Path(event.media_urls[0])
    assert cached.is_file()
    assert cached.read_bytes() == jpeg
    # The downstream classifier must see it as an image, not a document.
    image_paths, _audio, _audio_files, _video = GatewayInboundMixin._classify_inbound_media(event, False)
    assert image_paths == event.media_urls
    # The download went through the real TL message (not a mocked vision call).
    assert client.downloaded == [replied]


@pytest.mark.asyncio
async def test_guest_query_with_its_own_photo_carries_it_too():
    jpeg = _jpeg_bytes(color="green")
    client = _GuestClient(media_bytes=jpeg)
    seen = []

    async def handle(event):
        seen.append(event)
        return "I see a green photo"

    adapter = _guest_adapter(client, handle)
    query = _query_message(text="what is this?", media=types.MessageMediaPhoto(
        photo=_photo_message(message_id=500).media.photo,
    ))
    await adapter._on_guest_chat_query(_guest_update(query))

    assert len(seen) == 1
    assert seen[0].media_types == ["image/jpeg"]
    assert Path(seen[0].media_urls[0]).read_bytes() == jpeg


@pytest.mark.asyncio
async def test_guest_query_without_reply_stays_text_only():
    client = _GuestClient()
    seen = []

    async def handle(event):
        seen.append(event)
        return "no photo here"

    adapter = _guest_adapter(client, handle)
    await adapter._on_guest_chat_query(_guest_update(_query_message()))

    assert len(seen) == 1
    assert seen[0].media_urls == []
    assert seen[0].media_types == []
    assert client.downloaded == []


@pytest.mark.asyncio
async def test_guest_reply_target_missing_from_references_stays_text_only():
    client = _GuestClient()
    seen = []

    async def handle(event):
        seen.append(event)
        return "no photo here"

    adapter = _guest_adapter(client, handle)
    # The reply target (id 499) is not among the references: fail open to
    # today's text-only behaviour instead of guessing at another message.
    query = _query_message(reply_to_msg_id=499)
    await adapter._on_guest_chat_query(
        _guest_update(query, reference_messages=[_photo_message(message_id=123)])
    )

    assert len(seen) == 1
    assert seen[0].media_urls == []
    assert client.downloaded == []


@pytest.mark.asyncio
async def test_tl_document_image_rides_the_same_path():
    png = _png_bytes()
    client = _GuestClient(media_bytes=png)
    adapter = _guest_adapter(client, AsyncMock(return_value="ok"))
    paths, media_types, note = await adapter._cache_inbound_tl_media(
        _document_message(message_id=499)
    )
    assert note == ""
    assert len(paths) == 1
    assert media_types == ["image/png"]
    assert Path(paths[0]).read_bytes() == png


@pytest.mark.asyncio
async def test_tl_message_without_media_caches_nothing():
    client = _GuestClient()
    adapter = _guest_adapter(client, AsyncMock(return_value="ok"))
    paths, media_types, note = await adapter._cache_inbound_tl_media(_query_message())
    assert (paths, media_types, note) == ([], [], "")
    assert client.downloaded == []
