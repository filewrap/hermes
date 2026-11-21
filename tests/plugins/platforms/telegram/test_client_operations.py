"""send_message executes immediately for any admitted user: recipient validation, rich send path."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("telethon")

from telethon import functions, types

from gateway.config import Platform
from gateway.session import SessionSource
from plugins.platforms.telegram.client_operations import TelegramClientOperations


@pytest.fixture
def ops(tmp_path):
    adapter = SimpleNamespace(
        _client=object(),
        _authorized=lambda _source: True,
        _is_owner_user=lambda _user, source=None, **_kw: True,
        _settings_home=lambda _source: tmp_path / "alpha",
        _input_peer_for=AsyncMock(return_value="resolved-peer"),
        _mtproto_rich_request=AsyncMock(return_value=SimpleNamespace(updates=[])),
        _sent_message_id=lambda *args, **kwargs: 555,
        _message_thread_id=lambda _message: None,
    )
    return TelegramClientOperations(adapter), adapter


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="-10042", chat_type="group",
                         user_id="100", thread_id="77")


@pytest.mark.asyncio
async def test_send_message_executes_immediately(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(), "send_message",
        {"recipient": "@someone", "content": "Hello there", "format": "auto"})
    assert result["ok"] is True
    assert result["status"] == "complete"
    assert result["data"]["message_id"] == 555
    assert result["data"]["recipient"] == "@someone"
    assert result["data"]["rich_message"] is True
    adapter._mtproto_rich_request.assert_awaited_once()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.peer == "resolved-peer"
    assert request.message == "Hello there"
    assert request.reply_to is None
    resolved = [call.args[0] for call in adapter._input_peer_for.await_args_list]
    assert "@someone" in resolved
    assert resolved.count("@someone") == 2


@pytest.mark.asyncio
async def test_send_message_sends_rich_message_to_resolved_recipient(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(), "send_message",
        {"recipient": "someone", "content": "Hello there", "format": "auto"})
    assert result["ok"] is True
    assert result["status"] == "complete"
    assert result["data"]["message_id"] == 555
    assert result["data"]["recipient"] == "@someone"
    assert result["data"]["rich_message"] is True
    adapter._mtproto_rich_request.assert_awaited_once()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.peer == "resolved-peer"
    assert request.message == "Hello there"
    assert request.reply_to is None
    resolved = [call.args[0] for call in adapter._input_peer_for.await_args_list]
    assert "@someone" in resolved
    assert resolved.count("@someone") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient,normalized", [
    ("@someone", "@someone"), ("123456789", "123456789"), ("-1001234567890", "-1001234567890"),
])
async def test_send_message_accepts_username_and_numeric_recipients(ops, recipient, normalized):
    operations, _adapter = ops
    result = await operations.request(
        _source(), "send_message", {"recipient": recipient, "content": "Hello there"})
    assert result["ok"] is True and result["status"] == "complete", result
    assert result["data"]["recipient"] == normalized


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient", ["not a name", "http://example.com/x", "@ab", "", "tg://user?id=1", "@bad-name!"])
async def test_send_message_refuses_bad_recipient_before_any_send(ops, recipient):
    operations, adapter = ops
    result = await operations.request(
        _source(), "send_message", {"recipient": recipient, "content": "Hello there"})
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert "recipient" in result["error"]["message"]
    adapter._mtproto_rich_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_refuses_empty_or_oversize_content(ops):
    operations, adapter = ops
    for content in ("", "   ", "x" * 32_769):
        result = await operations.request(
            _source(), "send_message", {"recipient": "@someone", "content": content})
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_ARGUMENT"
    adapter._mtproto_rich_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_refuses_unresolvable_recipient_with_clear_error(ops):
    operations, adapter = ops

    async def _resolve(peer):
        if peer == "@ghost":
            raise ValueError("Cannot find any entity")
        return "resolved-peer"

    adapter._input_peer_for = AsyncMock(side_effect=_resolve)
    result = await operations.request(
        _source(), "send_message", {"recipient": "@ghost", "content": "Hello there"})
    assert result["ok"] is False
    assert "could not resolve recipient" in result["error"]["message"]
    assert "@ghost" in result["error"]["message"]
    adapter._mtproto_rich_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_is_available_to_admitted_nonowner(ops):
    operations, adapter = ops
    adapter._is_owner_user = lambda _user, source=None, **_kw: False
    result = await operations.request(
        _source(), "send_message", {"recipient": "@someone", "content": "Hello there"})
    assert result["ok"] is True and result["status"] == "complete", result
    assert result["data"]["recipient"] == "@someone"
    adapter._mtproto_rich_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_executes_one_allowlisted_read_on_the_live_client(ops, tmp_path):
    """Owner `request` makes exactly one call on the existing client, creating nothing."""
    operations, adapter = ops
    client = adapter._client
    adapter._input_peer_for = AsyncMock(
        return_value=types.InputPeerChannel(channel_id=42, access_hash=123))
    adapter._mtproto_rich_request = AsyncMock(return_value=SimpleNamespace(
        to_dict=lambda: {"full_chat": {"about": "Test channel"}, "chats": []}))
    result = await operations.request(
        _source(), "request", {"method": "channels.GetFullChannel", "args": {}})
    assert result["ok"] is True
    assert result["status"] == "complete"
    assert result["action"] == "request"
    assert result["data"]["method"] == "channels.GetFullChannel"
    assert result["data"]["result"] == {"full_chat": {"about": "Test channel"}, "chats": []}
    adapter._mtproto_rich_request.assert_awaited_once()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.channels.GetFullChannelRequest)
    assert request.channel.channel_id == 42
    assert request.channel.access_hash == 123
    assert adapter._client is client
    assert not (tmp_path / "alpha" / "sessions").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args,request_cls", [
    ("channels.DeleteChannelRequest", {}, functions.channels.DeleteChannelRequest),
    ("account.DeleteAccountRequest", {"reason": "leaving"}, functions.account.DeleteAccountRequest),
])
async def test_request_builds_and_dispatches_previously_blocked_methods(ops, method, args, request_cls):
    """The request blocklist is gone: any TL method name builds and dispatches."""
    operations, adapter = ops
    adapter._input_peer_for = AsyncMock(
        return_value=types.InputPeerChannel(channel_id=42, access_hash=123))
    result = await operations.request(_source(), "request", {"method": method, "args": args})
    assert result["ok"] is True and result["status"] == "complete", result
    assert result["data"]["method"] == method.removesuffix("Request")
    adapter._mtproto_rich_request.assert_awaited_once()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, request_cls)
    if isinstance(request, functions.channels.DeleteChannelRequest):
        assert request.channel.channel_id == 42
    if isinstance(request, functions.account.DeleteAccountRequest):
        assert request.reason == "leaving"


@pytest.mark.asyncio
async def test_request_builds_and_dispatches_log_out(ops):
    """auth.LogOutRequest has a var-args constructor; the generic builder skips
    var-positional/var-keyword params, so it builds with no args and dispatches."""
    operations, adapter = ops
    result = await operations.request(_source(), "request", {"method": "auth.LogOutRequest", "args": {}})
    assert result["ok"] is True and result["status"] == "complete", result
    assert result["data"]["method"] == "auth.LogOut"
    adapter._mtproto_rich_request.assert_awaited_once()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.auth.LogOutRequest)
# ---- Generic send_message: reply/thread/silent/effect/buttons/pin/media/location ----


@pytest.mark.asyncio
async def test_send_message_reply_thread_silent_effect(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(),
        "send_message",
        {
            "recipient": "@someone",
            "content": "Hello there",
            "reply_to": 12,
            "thread_id": 99,
            "silent": True,
            "effect": 512,
        },
    )
    assert result["ok"] is True and result["status"] == "complete", result
    assert result["data"]["message_id"] == 555
    assert result["data"]["reply_to_message_id"] == 12
    assert result["data"]["thread_id"] == "99"
    assert result["data"]["silent"] is True
    assert result["data"]["effect"] == 512
    adapter._mtproto_rich_request.assert_awaited_once()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.reply_to.reply_to_msg_id == 12
    assert request.reply_to.top_msg_id == 99
    assert request.silent is True
    assert request.effect == 512


@pytest.mark.asyncio
async def test_send_message_with_buttons(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(),
        "send_message",
        {
            "recipient": "@someone",
            "content": "Hello there",
            "format": "html",
            "buttons": [
                {"type": "url", "label": "Open", "value": "https://example.com"},
                {"type": "copy", "label": "Copy", "value": "copied"},
            ],
        },
    )
    assert result["ok"] is True and result["status"] == "complete", result
    assert result["data"]["button_count"] == 2
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.message.startswith("Hello there")


@pytest.mark.asyncio
async def test_send_message_pins_after_send(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(),
        "send_message",
        {"recipient": "@someone", "content": "Hello there", "pin": True},
    )
    assert result["ok"] is True and result["status"] == "complete", result
    assert result["data"]["pinned"] is True
    assert adapter._mtproto_rich_request.await_count == 2
    first, second = (
        call.args[0] for call in adapter._mtproto_rich_request.await_args_list
    )
    assert isinstance(first, functions.messages.SendMessageRequest)
    assert isinstance(second, functions.messages.UpdatePinnedMessageRequest)
    assert second.id == 555
    assert second.silent is True


@pytest.mark.asyncio
async def test_send_message_pin_failure_keeps_send(ops):
    operations, adapter = ops
    adapter._mtproto_rich_request = AsyncMock(
        side_effect=[SimpleNamespace(updates=[]), RuntimeError("CHAT_ADMIN_REQUIRED")]
    )
    result = await operations.request(
        _source(),
        "send_message",
        {"recipient": "@someone", "content": "Hello there", "pin": True},
    )
    assert result["ok"] is True and result["status"] == "complete", result
    assert result["data"]["message_id"] == 555
    assert result["data"]["pinned"] is False
    assert "pin_error" in result["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"recipient": "@someone", "content": "Hi", "quote": "excerpt"},
        {"recipient": "@someone", "content": "Hi", "video_note": True},
        {"recipient": "@someone", "content": "Hi", "as_voice": True},
        {
            "recipient": "@someone",
            "content": "Hi",
            "media": "data:image/png;base64,AAAA",
            "location": {"latitude": 1.0, "longitude": 2.0},
        },
        {
            "recipient": "@someone",
            "content": "Hi",
            "location": {"latitude": 91.0, "longitude": 2.0},
        },
        {
            "recipient": "@someone",
            "content": "Hi",
            "location": {"latitude": 1.0, "longitude": 2.0, "name": "Place"},
        },
        {"recipient": "@someone", "content": "Hi", "silent": "yes"},
        {"recipient": "@someone", "content": "Hi", "effect": -1},
        {"recipient": "@someone", "content": "Hi", "unknown_param": True},
    ],
)
async def test_send_message_refuses_invalid_send_params(ops, params):
    operations, adapter = ops
    result = await operations.request(_source(), "send_message", params)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    adapter._mtproto_rich_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_quote_reads_target(ops):
    operations, adapter = ops
    adapter._client = SimpleNamespace(
        get_messages=AsyncMock(
            return_value=[SimpleNamespace(id=12, message="hello world")]
        ),
        get_input_entity=AsyncMock(
            return_value=types.InputUser(user_id=100, access_hash=1)
        ),
    )
    result = await operations.request(
        _source(),
        "send_message",
        {
            "recipient": "@someone",
            "content": "Replying",
            "reply_to": 12,
            "quote": "world",
        },
    )
    assert result["ok"] is True and result["status"] == "complete", result
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert request.reply_to.reply_to_msg_id == 12
    assert request.reply_to.quote_text == "world"
    assert request.reply_to.quote_offset == 6


@pytest.mark.asyncio
async def test_send_message_quote_must_match_target_text(ops):
    operations, adapter = ops
    adapter._client = SimpleNamespace(
        get_messages=AsyncMock(
            return_value=[SimpleNamespace(id=12, message="hello world")]
        )
    )
    result = await operations.request(
        _source(),
        "send_message",
        {
            "recipient": "@someone",
            "content": "Replying",
            "reply_to": 12,
            "quote": "goodbye",
        },
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert "quote must match" in result["error"]["message"]
    adapter._mtproto_rich_request.assert_not_awaited()


_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8Xw8AAwMCAO+ip1sAAAAASUVORK5CYII="


@pytest.mark.asyncio
async def test_send_message_media_base64(ops):
    operations, adapter = ops
    adapter._client = SimpleNamespace(
        upload_file=AsyncMock(
            return_value=types.InputFile(
                id=1, parts=1, name="tiny.png", md5_checksum=""
            )
        ),
        get_input_entity=AsyncMock(
            return_value=types.InputUser(user_id=100, access_hash=1)
        ),
    )
    result = await operations.request(
        _source(),
        "send_message",
        {
            "recipient": "@someone",
            "content": "Look at this",
            "media": f"data:image/png;base64,{_TINY_PNG_B64}",
            "filename": "tiny.png",
        },
    )
    assert result["ok"] is True and result["status"] == "complete", result
    assert result["data"]["message_id"] == 555
    assert result["data"]["media"] is True
    adapter._mtproto_rich_request.assert_awaited_once()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.messages.SendMediaRequest)
    assert isinstance(request.media, types.InputMediaUploadedPhoto)
    assert request.message == "Look at this"


@pytest.mark.asyncio
async def test_send_message_location(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(),
        "send_message",
        {
            "recipient": "@someone",
            "location": {"latitude": 28.6139, "longitude": 77.2090, "accuracy": 50},
        },
    )
    assert result["ok"] is True and result["status"] == "complete", result
    assert result["data"]["message_id"] == 555
    assert result["data"]["location"] is True
    adapter._mtproto_rich_request.assert_awaited_once()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.messages.SendMediaRequest)
    assert isinstance(request.media, types.InputMediaGeoPoint)
    assert request.media.geo_point.lat == 28.6139
    assert request.media.geo_point.long == 77.2090
    assert request.media.geo_point.accuracy_radius == 50
    assert request.message == ""


@pytest.mark.asyncio
async def test_send_message_venue(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(),
        "send_message",
        {
            "recipient": "@someone",
            "location": {
                "latitude": 28.6139,
                "longitude": 77.2090,
                "name": "India Gate",
                "address": "New Delhi",
            },
        },
    )
    assert result["ok"] is True and result["status"] == "complete", result
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.messages.SendMediaRequest)
    assert isinstance(request.media, types.InputMediaVenue)
    assert request.media.title == "India Gate"
    assert request.media.address == "New Delhi"
