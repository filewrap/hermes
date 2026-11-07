"""Behaviour contracts for Telegram widget sends (layer 233).

Covers the send-boundary validation and request builders in
``widget_messages.py`` plus the adapter's ``send_widget`` / ``upload_widget``
wiring: a widget that breaks the server limits is refused here, a valid one
serialises to a real wire request, and broadcast channels are refused while
supergroups are not.
"""
import os
import zlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from plugins.platforms.telegram.widget_messages import (
    build_widget_media,
    is_broadcast_channel,
    send_widget_request,
    sent_widget_info,
    validate_widget_html,
    validate_widget_result,
    validate_widget_state,
    widget_html_from_media,
)

HTML = "<div><h1>Hi</h1><button>Tap</button></div>"


def test_oversize_raw_html_is_refused():
    with pytest.raises(ValueError, match="32,768"):
        validate_widget_html("a" * 32_769)


def test_incompressible_html_over_compressed_limit_is_refused():
    # 6,000 random bytes stay over 4,096 compressed while staying well
    # under the 32,768 raw limit: the two limits are independent.
    raw = os.urandom(6_000).decode("latin-1")
    assert len(raw.encode("utf-8")) < 32_768
    assert len(zlib.compress(raw.encode("utf-8"), 9)) > 4_096
    with pytest.raises(ValueError, match="4,096"):
        validate_widget_html(raw)


def test_oversize_state_and_answer_are_refused():
    with pytest.raises(ValueError, match="1,024"):
        validate_widget_state("x" * 1_025, field="public_data")
    with pytest.raises(ValueError, match="1,024"):
        validate_widget_state("x" * 1_025, field="chat_data")
    with pytest.raises(ValueError, match="1,024"):
        validate_widget_result("x" * 1_025)
    assert validate_widget_result(None) is None
    assert validate_widget_state(None, field="public_data") is None


def test_fixed_dimensions_outside_the_bubble_range_are_refused():
    for width, height in ((100, 0), (601, 0), (0, 49), (0, 601)):
        with pytest.raises(ValueError):
            build_widget_media(HTML, width=width, height=height)
    assert build_widget_media(HTML, width=0, height=0).width == 0
    assert build_widget_media(HTML, width=320, height=200).width == 320


def test_widget_send_request_round_trips_on_the_wire():
    from telethon import types
    from telethon.extensions.binaryreader import BinaryReader

    peer = types.InputPeerUser(user_id=8495104954, access_hash=12345)
    media = build_widget_media(
        HTML, allow_queries=True, shared_widget=True, public_data='{"r":1}',
    )
    request = send_widget_request(peer, media, message="Vote in the widget")
    assert not hasattr(request, "rich_message")
    assert request.media is media
    assert request.message == "Vote in the widget"
    data = bytes(request)
    back = BinaryReader(data).tgread_object()
    assert bytes(back) == data
    assert back.media.html == HTML
    assert back.media.allow_queries is True
    assert back.media.shared_widget is True


def test_widget_html_decompresses_from_received_media():
    from telethon import types

    media = types.MessageMediaWidget(
        width=0, height=0, payload=zlib.compress(HTML.encode("utf-8"), 9),
        widget_id=123,
    )
    assert widget_html_from_media(media) == HTML
    with pytest.raises(TypeError):
        widget_html_from_media(types.MessageMediaEmpty())


def test_sent_widget_info_extracts_message_and_widget_ids():
    from telethon import types

    media = types.MessageMediaWidget(width=0, height=0, payload=b"", widget_id=987654321)
    message = types.Message(id=77, peer_id=types.PeerUser(user_id=42), date=None, message="cap", media=media)
    result = SimpleNamespace(updates=[types.UpdateNewMessage(message=message, pts=1, pts_count=1)])
    assert sent_widget_info(result) == ("77", 987654321)
    assert sent_widget_info(SimpleNamespace(updates=[])) == (None, None)


def test_broadcast_channel_is_refused_but_supergroup_is_not():
    from telethon import types

    def channel(*, megagroup):
        return types.Channel(
            id=999, title="t", photo=types.ChatPhotoEmpty(), date=None,
            megagroup=megagroup,
        )

    assert is_broadcast_channel(channel(megagroup=False)) is True
    assert is_broadcast_channel(channel(megagroup=True)) is False
    assert is_broadcast_channel(channel(megagroup=None)) is True
    chat = types.Chat(id=7, title="g", photo=types.ChatPhotoEmpty(),
                      participants_count=2, date=datetime.now(UTC), version=1)
    assert is_broadcast_channel(chat) is False
    assert is_broadcast_channel(object()) is False


def _widget_updates(message_id=77, widget_id=987654321):
    from telethon import types

    media = types.MessageMediaWidget(width=0, height=0, payload=b"", widget_id=widget_id)
    message = types.Message(id=message_id, peer_id=types.PeerUser(user_id=42),
                            date=None, message="cap", media=media)
    return SimpleNamespace(updates=[types.UpdateNewMessage(message=message, pts=1, pts_count=1)])


class _WidgetFakeClient:
    """Fake Telethon client: resolves peers, answers entity lookups, sends."""

    def __init__(self, *, entity=None, reply=None):
        self.entity = entity
        self.reply = reply
        self.sent = []

    async def get_input_entity(self, peer):
        from telethon import types

        if isinstance(peer, int) and peer < 0:
            return types.InputPeerChannel(channel_id=abs(peer), access_hash=123)
        return types.InputPeerUser(user_id=int(peer), access_hash=123)

    async def get_entity(self, peer):
        return self.entity

    async def __call__(self, request):
        self.sent.append(request)
        return self.reply if self.reply is not None else _widget_updates()


@pytest.mark.asyncio
async def test_adapter_send_widget_delivers_and_reports_ids():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = _WidgetFakeClient()

    result = await adapter.send_widget("42", HTML, caption="Vote in the widget",
                                       allow_queries=True, shared_widget=True,
                                       public_data='{"r":1}')

    assert result.success
    assert result.message_id == "77"
    assert result.raw_response == {"widget_id": 987654321}
    request = adapter._client.sent[0]
    assert type(request).__name__ == "SendMediaRequest"
    assert type(request.media).__name__ == "InputMediaWidget"
    assert request.media.html == HTML
    assert request.message == "Vote in the widget"


@pytest.mark.asyncio
async def test_adapter_send_widget_refuses_broadcast_channel():
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    broadcast = types.Channel(id=999, title="c", photo=types.ChatPhotoEmpty(),
                              date=None, megagroup=False)
    adapter._client = _WidgetFakeClient(entity=broadcast)

    result = await adapter.send_widget("-100999", HTML)

    assert not result.success
    assert "channel" in (result.error or "").lower()
    assert adapter._client.sent == []


@pytest.mark.asyncio
async def test_adapter_upload_widget_reports_id_without_sending():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = _WidgetFakeClient()

    result = await adapter.upload_widget("42", HTML, allow_queries=True)

    assert result.success
    request = adapter._client.sent[0]
    assert type(request).__name__ == "UploadMediaRequest"
    assert type(request.media).__name__ == "InputMediaWidget"


@pytest.mark.asyncio
async def test_adapter_send_widget_refuses_oversize_html():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = _WidgetFakeClient()

    result = await adapter.send_widget("42", "a" * 32_769)

    assert not result.success
    assert "32,768" in (result.error or "")
    assert adapter._client.sent == []


@pytest.mark.asyncio
async def test_adapter_send_widget_by_id_delivers_and_reports_ids():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = _WidgetFakeClient()

    result = await adapter.send_widget_by_id("42", 987654321, caption="The same poll, here too",
                                             chat_data='{"my":0}')

    assert result.success
    assert result.message_id == "77"
    assert result.raw_response == {"widget_id": 987654321}
    request = adapter._client.sent[0]
    assert type(request).__name__ == "SendMediaRequest"
    assert type(request.media).__name__ == "InputMediaWidgetId"
    assert request.media.widget_id == 987654321
    assert request.media.chat_data == '{"my":0}'
    assert request.message == "The same poll, here too"


@pytest.mark.asyncio
async def test_adapter_send_widget_by_id_refuses_broadcast_channel():
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    broadcast = types.Channel(id=999, title="c", photo=types.ChatPhotoEmpty(),
                              date=None, megagroup=False)
    adapter._client = _WidgetFakeClient(entity=broadcast)

    result = await adapter.send_widget_by_id("-100999", 987654321)

    assert not result.success
    assert "channel" in (result.error or "").lower()
    assert adapter._client.sent == []


@pytest.mark.asyncio
async def test_adapter_answer_widget_query_sends_answer():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = _WidgetFakeClient()

    assert await adapter.answer_widget_query(987654321, '{"ok": true}') is True
    assert await adapter.answer_widget_query(987654321) is True
    requests = adapter._client.sent
    assert [type(r).__name__ for r in requests] == [
        "SetBotWidgetQueryAnswerRequest", "SetBotWidgetQueryAnswerRequest",
    ]
    assert requests[0].query_id == 987654321
    assert requests[0].result == '{"ok": true}'
    assert requests[1].result is None


@pytest.mark.asyncio
async def test_adapter_answer_widget_query_false_when_disconnected():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    assert await adapter.answer_widget_query(1) is False


@pytest.mark.asyncio
async def test_adapter_edit_shared_and_message_widget_data():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = _WidgetFakeClient()

    result = await adapter.edit_shared_widget_data(987654321, '{"v":[3,5],"r":2}', cache_time=30)
    assert result.success
    assert type(adapter._client.sent[0]).__name__ == "EditWidgetDataRequest"
    assert adapter._client.sent[0].cache_time == 30

    result = await adapter.edit_widget_message_data("42", "77", public_data='{"r":2}')
    assert result.success
    assert type(adapter._client.sent[1]).__name__ == "EditMessageWidgetDataRequest"
    assert adapter._client.sent[1].id == 77

    result = await adapter.edit_widget_message_data("42", "77")
    assert not result.success
    assert "public_data or chat_data" in (result.error or "")


@pytest.mark.asyncio
async def test_adapter_edit_inline_widget_data():
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = _WidgetFakeClient()
    inline_id = types.InputBotInlineMessageID(dc_id=2, id=777, access_hash=999)

    result = await adapter.edit_inline_widget_data(inline_id, public_data='{"r":2}')

    assert result.success
    assert type(adapter._client.sent[0]).__name__ == "EditInlineBotMessageWidgetDataRequest"
    assert adapter._client.sent[0].id is inline_id


@pytest.mark.asyncio
async def test_adapter_get_widget_public_data_returns_server_result():
    from telethon import types

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    public = types.messages.WidgetPublicData(hash=42, data='{"r":2}', period=30)
    adapter._client = _WidgetFakeClient(reply=public)

    result = await adapter.get_widget_public_data("42", "77")

    assert result is public
    assert type(adapter._client.sent[0]).__name__ == "GetWidgetPublicDataRequest"
    assert adapter._client.sent[0].hash == 0
