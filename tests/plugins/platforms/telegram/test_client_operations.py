"""Owner-only send_message: confirmation gate, recipient validation, rich send path."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
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
async def test_send_message_returns_confirmation_not_a_send(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(), "send_message",
        {"recipient": "@someone", "content": "Hello there", "format": "auto"})
    assert result["ok"] is True
    assert result["status"] == "confirmation_required"
    assert result["params"]["recipient"] == "@someone"
    adapter._mtproto_rich_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_confirmed_sends_rich_message_to_resolved_recipient(ops):
    operations, adapter = ops
    params = {"recipient": "someone", "content": "Hello there", "format": "auto"}
    confirmed = await operations.request(_source(), "send_message", params)
    assert confirmed["status"] == "confirmation_required"
    result = await operations.execute_confirmed(_source(), "send_message", confirmed["params"])
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
@pytest.mark.parametrize("recipient", ["@someone", "123456789", "-1001234567890"])
async def test_send_message_accepts_username_and_numeric_recipients(ops, recipient):
    operations, _adapter = ops
    result = await operations.request(
        _source(), "send_message", {"recipient": recipient, "content": "Hello there"})
    assert result["status"] == "confirmation_required", result
    assert result["params"]["recipient"]


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
    confirmed = await operations.request(
        _source(), "send_message", {"recipient": "@ghost", "content": "Hello there"})
    assert confirmed["status"] == "confirmation_required"
    result = await operations.execute_confirmed(_source(), "send_message", confirmed["params"])
    assert result["ok"] is False
    assert "could not resolve recipient" in result["error"]["message"]
    assert "@ghost" in result["error"]["message"]
    adapter._mtproto_rich_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_is_owner_only(ops):
    operations, adapter = ops
    adapter._is_owner_user = lambda _user, source=None, **_kw: False
    result = await operations.request(
        _source(), "send_message", {"recipient": "@someone", "content": "Hello there"})
    assert result["ok"] is False
    assert result["error"]["code"] == "FORBIDDEN"
    adapter._mtproto_rich_request.assert_not_awaited()


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
@pytest.mark.parametrize("method", ["auth.LogOutRequest", "channels.DeleteChannelRequest",
                                    "account.DeleteAccountRequest"])
async def test_request_refuses_blocked_method_before_any_transport_call(ops, method):
    operations, adapter = ops
    result = await operations.request(_source(), "request", {"method": method, "args": {}})
    assert result["ok"] is False
    assert result["error"]["code"] == "REQUEST_BLOCKED"
    adapter._mtproto_rich_request.assert_not_awaited()
