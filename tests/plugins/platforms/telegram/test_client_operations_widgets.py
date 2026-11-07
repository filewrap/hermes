"""Owner-only widget actions on the telegram_client tool surface.

Covers the widget actions in ``TelegramClientOperations``: each action sends
through the adapter bound to the current chat only, validates its payload
before any transport call, and is refused to non-owners. The ``request``
action's allowlist stays read-only: widget TL methods are refused there.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types
from telethon.tl.types import messages as tl_messages

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from plugins.platforms.telegram.client_operations import TelegramClientOperations

HTML = "<div><h1>Hi</h1><button>Tap</button></div>"


@pytest.fixture
def ops(tmp_path):
    adapter = SimpleNamespace(
        _client=object(),
        _authorized=lambda _source: True,
        _is_owner_user=lambda _user, source=None, **_kw: True,
        _settings_home=lambda _source: tmp_path / "alpha",
        _input_peer_for=AsyncMock(return_value="resolved-peer"),
        send_widget=AsyncMock(return_value=SendResult(
            success=True, message_id="77", raw_response={"widget_id": 987654321})),
        send_widget_by_id=AsyncMock(return_value=SendResult(
            success=True, message_id="78", raw_response={"widget_id": 987654321})),
        upload_widget=AsyncMock(return_value=SendResult(
            success=True, raw_response={"widget_id": 987654321})),
        answer_widget_query=AsyncMock(return_value=True),
        edit_widget_message_data=AsyncMock(return_value=SendResult(success=True)),
        edit_shared_widget_data=AsyncMock(return_value=SendResult(success=True)),
        edit_inline_widget_data=AsyncMock(return_value=SendResult(success=True)),
        get_widget_public_data=AsyncMock(return_value=tl_messages.WidgetPublicData(
            hash=42, data='{"r":2}', period=30)),
    )
    return TelegramClientOperations(adapter), adapter


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="-10042", chat_type="group",
                         user_id="100", thread_id="77")


@pytest.mark.asyncio
async def test_widget_send_delivers_to_current_chat_and_reports_ids(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(), "widget_send",
        {"html": HTML, "caption": "Vote in the widget", "allow_queries": True,
         "shared_widget": True, "public_data": '{"r":1}'})
    assert result["ok"] is True, result
    assert result["status"] == "complete"
    assert result["data"]["message_id"] == "77"
    assert result["data"]["widget_id"] == 987654321
    adapter.send_widget.assert_awaited_once()
    call = adapter.send_widget.await_args
    assert call.args[0] == "-10042"  # peer forced to the current chat
    assert call.args[1] == HTML
    assert call.kwargs["caption"] == "Vote in the widget"
    assert call.kwargs["allow_queries"] is True
    assert call.kwargs["shared_widget"] is True


@pytest.mark.asyncio
async def test_widget_send_builds_classic_keyboard_below_widget(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(), "widget_send",
        {"html": HTML, "reply_markup": [[["Open results", "open:results", "primary"],
                                         ["Stop", "stop", None]]]})
    assert result["ok"] is True, result
    markup = adapter.send_widget.await_args.kwargs["reply_markup"]
    assert isinstance(markup, types.ReplyInlineMarkup)
    buttons = markup.rows[0].buttons
    assert [button.text for button in buttons] == ["Open results", "Stop"]
    assert buttons[0].type.data == b"open:results"


@pytest.mark.asyncio
async def test_widget_send_refuses_oversize_html_before_any_adapter_call(ops):
    operations, adapter = ops
    result = await operations.request(_source(), "widget_send", {"html": "a" * 32_769})
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert "32,768" in result["error"]["message"]
    adapter.send_widget.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action,params", [
    ("widget_send", {"html": HTML}),
    ("widget_get_public_data", {"message_id": 77}),
])
async def test_widget_actions_are_owner_only(ops, action, params):
    operations, adapter = ops
    adapter._is_owner_user = lambda _user, source=None, **_kw: False
    result = await operations.request(_source(), action, params)
    assert result["ok"] is False
    assert result["error"]["code"] == "FORBIDDEN"
    adapter.send_widget.assert_not_awaited()
    adapter.get_widget_public_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_widget_send_by_id_resends_shared_object_to_current_chat(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(), "widget_send_by_id",
        {"widget_id": 987654321, "chat_data": '{"my":0}', "caption": "The same poll, here too"})
    assert result["ok"] is True, result
    assert result["data"]["message_id"] == "78"
    assert result["data"]["widget_id"] == 987654321
    adapter.send_widget_by_id.assert_awaited_once()
    call = adapter.send_widget_by_id.await_args
    assert call.args[0] == "-10042"
    assert call.args[1] == 987654321
    assert call.kwargs["chat_data"] == '{"my":0}'


@pytest.mark.asyncio
async def test_widget_upload_returns_widget_id_without_sending(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(), "widget_upload", {"html": HTML, "allow_queries": True})
    assert result["ok"] is True, result
    assert result["data"]["widget_id"] == 987654321
    adapter.upload_widget.assert_awaited_once()
    adapter.send_widget.assert_not_awaited()


@pytest.mark.asyncio
async def test_widget_answer_accepts_empty_and_short_result(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(), "widget_answer", {"query_id": 987654321, "result": '{"ok": true}'})
    assert result["ok"] is True, result
    assert result["data"]["answered"] is True
    empty = await operations.request(_source(), "widget_answer", {"query_id": 987654321})
    assert empty["ok"] is True, empty
    assert [call.args[1] for call in adapter.answer_widget_query.await_args_list] == [
        '{"ok": true}', None]


@pytest.mark.asyncio
async def test_widget_answer_refuses_oversize_result(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(), "widget_answer", {"query_id": 1, "result": "x" * 1_025})
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert "1,024" in result["error"]["message"]
    adapter.answer_widget_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_widget_edit_message_requires_state_and_edits_current_chat(ops):
    operations, adapter = ops
    refused = await operations.request(
        _source(), "widget_edit_message", {"message_id": 77})
    assert refused["ok"] is False
    assert refused["error"]["code"] == "INVALID_ARGUMENT"
    adapter.edit_widget_message_data.assert_not_awaited()
    result = await operations.request(
        _source(), "widget_edit_message", {"message_id": 77, "public_data": '{"r":2}'})
    assert result["ok"] is True, result
    assert result["data"]["message_id"] == 77
    call = adapter.edit_widget_message_data.await_args
    assert call.args[0] == "-10042"
    assert call.args[1] == 77


@pytest.mark.asyncio
async def test_widget_edit_shared_requires_public_data_and_validates_cache_time(ops):
    operations, adapter = ops
    refused = await operations.request(
        _source(), "widget_edit_shared", {"widget_id": 987654321})
    assert refused["ok"] is False
    assert refused["error"]["code"] == "INVALID_ARGUMENT"
    bad_cache = await operations.request(
        _source(), "widget_edit_shared",
        {"widget_id": 987654321, "public_data": '{"r":2}', "cache_time": -1})
    assert bad_cache["ok"] is False
    assert bad_cache["error"]["code"] == "INVALID_ARGUMENT"
    adapter.edit_shared_widget_data.assert_not_awaited()
    result = await operations.request(
        _source(), "widget_edit_shared",
        {"widget_id": 987654321, "public_data": '{"v":[3,5],"r":2}', "cache_time": 30})
    assert result["ok"] is True, result
    assert result["data"]["widget_id"] == 987654321
    call = adapter.edit_shared_widget_data.await_args
    assert call.args[0] == 987654321
    assert call.kwargs["cache_time"] == 30


@pytest.mark.asyncio
async def test_widget_edit_inline_validates_inline_message_id_shape(ops):
    operations, adapter = ops
    for bad_id in ({"dc_id": 2, "id": 777}, {"dc_id": 2.5, "id": 777, "access_hash": 999},
                   {"dc_id": 2, "id": 777, "access_hash": 999, "extra": 1}):
        result = await operations.request(
            _source(), "widget_edit_inline",
            {"inline_message_id": bad_id, "public_data": '{"r":2}'})
        assert result["ok"] is False, result
        assert result["error"]["code"] == "INVALID_ARGUMENT"
    adapter.edit_inline_widget_data.assert_not_awaited()
    result = await operations.request(
        _source(), "widget_edit_inline",
        {"inline_message_id": {"dc_id": 2, "id": 777, "access_hash": 999},
         "public_data": '{"r":2}'})
    assert result["ok"] is True, result
    inline_id = adapter.edit_inline_widget_data.await_args.args[0]
    assert isinstance(inline_id, types.InputBotInlineMessageID)
    assert (inline_id.dc_id, inline_id.id, inline_id.access_hash) == (2, 777, 999)


@pytest.mark.asyncio
async def test_widget_get_public_data_returns_data_period_and_not_modified(ops):
    operations, adapter = ops
    result = await operations.request(
        _source(), "widget_get_public_data", {"message_id": 77})
    assert result["ok"] is True, result
    assert result["data"] == {"data": '{"r":2}', "period": 30, "hash": 42,
                              "not_modified": False}
    adapter.get_widget_public_data.assert_awaited_once()
    call = adapter.get_widget_public_data.await_args
    assert call.args[0] == "-10042"
    assert call.args[1] == 77
    assert call.kwargs["hash"] == 0
    adapter.get_widget_public_data = AsyncMock(return_value=tl_messages.WidgetPublicDataNotModified(period=30))
    stale = await operations.request(
        _source(), "widget_get_public_data", {"message_id": 77, "hash": 42})
    assert stale["ok"] is True, stale
    assert stale["data"] == {"not_modified": True, "period": 30}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["messages.SendMediaRequest", "messages.EditWidgetDataRequest",
                                    "messages.SetBotWidgetQueryAnswerRequest"])
async def test_request_still_refuses_widget_methods(ops, method):
    operations, adapter = ops
    result = await operations.request(_source(), "request", {"method": method, "args": {}})
    assert result["ok"] is False
    assert result["error"]["code"] == "REQUEST_BLOCKED"
    adapter.send_widget.assert_not_awaited()
    adapter.edit_shared_widget_data.assert_not_awaited()
    adapter.answer_widget_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_capabilities_lists_widget_actions_for_owner(ops):
    operations, _adapter = ops
    result = await operations.request(_source(), "capabilities", {})
    assert result["ok"] is True, result
    assert {"widget_send", "widget_send_by_id", "widget_upload", "widget_answer",
            "widget_edit_message", "widget_edit_shared", "widget_edit_inline",
            "widget_get_public_data"} <= set(result["data"]["direct_actions"])
    assert "widget_send" in result["data"]["parameters"]
    assert "html" in result["data"]["parameters"]["widget_send"]
