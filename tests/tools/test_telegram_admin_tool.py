import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.session_context import clear_session_vars, set_session_vars


def _dispatch_in_session(monkeypatch, *, platform="telegram", chat_id="-10042", thread_id="80", args):
    import tools.send_message_senders as senders
    from model_tools import handle_function_call

    fake_adapter = SimpleNamespace(
        _client=object(),
        list_chat_messages=AsyncMock(return_value={
            "messages": [{"message_id": "77", "text": "candidate"}],
            "next_offset_id": "77", "scanned_count": 1,
        }),
        pin_chat_message=AsyncMock(return_value={"message_id": "77", "pinned": True}),
    )
    monkeypatch.setattr(senders, "_live_adapter", lambda _platform: (object(), fake_adapter))
    tokens = set_session_vars(platform=platform, chat_id=chat_id, thread_id=thread_id)
    try:
        result = json.loads(handle_function_call("telegram_chat_admin", args))
    finally:
        clear_session_vars(tokens)
    return result, fake_adapter


def test_registry_dispatch_lists_messages_from_active_chat_with_thread_context(monkeypatch):
    result, adapter = _dispatch_in_session(
        monkeypatch,
        args={"action": "list_messages", "limit": 5, "offset_id": "120"},
    )

    assert result["success"] is True
    assert result["chat_id"] == "-10042"
    assert result["current_thread_id"] == "80"
    assert result["current_thread_only"] is True
    assert result["next_offset_id"] == "77"
    assert result["scanned_count"] == 1
    adapter.list_chat_messages.assert_awaited_once_with(
        "-10042", limit=5, offset_id=120, pinned_only=False, query=None, thread_id="80",
    )


def test_registry_dispatch_pins_only_in_active_chat_even_if_other_chat_id_is_passed(monkeypatch):
    result, adapter = _dispatch_in_session(
        monkeypatch,
        args={"action": "pin_message", "message_id": "77", "chat_id": "-99999"},
    )

    assert result["success"] is True
    adapter.pin_chat_message.assert_awaited_once_with("-10042", "77", unpin=False)


def test_registry_dispatch_denies_non_telegram_session(monkeypatch):
    result, adapter = _dispatch_in_session(
        monkeypatch, platform="discord", args={"action": "pin_message", "message_id": "77"},
    )

    assert "error" in result
    assert "only inside a Telegram" in result["error"]
    adapter.pin_chat_message.assert_not_awaited()


def test_registry_dispatch_can_search_pinned_messages_in_active_topic(monkeypatch):
    result, adapter = _dispatch_in_session(
        monkeypatch,
        args={"action": "list_pinned_messages", "query": "equation", "limit": 7},
    )

    assert result["success"] is True
    adapter.list_chat_messages.assert_awaited_once_with(
        "-10042", limit=7, offset_id=0, pinned_only=True, query="equation", thread_id="80",
    )


def test_registered_tool_has_no_arbitrary_chat_target():
    from tools import telegram_admin_tool
    from model_tools import get_toolset_for_tool

    assert get_toolset_for_tool("telegram_chat_admin") == "telegram_admin"
    assert "chat_id" not in telegram_admin_tool.TELEGRAM_ADMIN_SCHEMA["parameters"]["properties"]
