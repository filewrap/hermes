"""F4 conversation tools: list from cached sources, turn returns the real reply.

Live-check shape, in the sandbox: conversations_list merges live dialogs
with the durable registry; conversations_turn sends through the (stubbed)
telegram_client path, then the adapter hook's notify delivers the other
side's reply and the tool returns that reply — not the send receipt. Zero
LLM calls; no live Telegram connection.
"""

import json
import threading
import uuid
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from tools import conversation_registry as registry
from tools.conversation_reply_wait import notify_inbound


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "STORE_PATH", tmp_path / "conversations.json")
    return registry


def _agent():
    return SimpleNamespace(
        session_id=f"s-test-{uuid.uuid4().hex[:8]}",
        _interrupt_requested=False, _yield_requested=None,
        _touch_activity=lambda _desc: None,
    )


def _ctx():
    return InlineToolContext(effective_task_id="t-1", tool_call_id="call-1")


def _call(name, agent, args):
    return json.loads(INLINE_TOOL_EXECUTORS[name](agent, args, _ctx()))


@pytest.fixture
def live_dialogs(monkeypatch):
    """Stub the live-dialogs fetch + Telegram turn context for this module."""
    import tools.conversation_tools as tools_mod

    monkeypatch.setattr(
        tools_mod, "_live_dialogs",
        lambda limit, chat_id, user_id: [
            {"id": "-10042", "name": "Testing Agents", "type": "group",
             "username": None, "unread_count": 0, "last_message": "hi"},
            {"id": "100", "name": "Ann", "type": "dm",
             "username": "ann", "unread_count": 1, "last_message": "yo"},
        ][:limit])
    monkeypatch.setattr(
        tools_mod, "_telegram_context", lambda: ("-10042", "100", None))
    return tools_mod


class TestConversationsList:
    def test_live_dialogs_get_refs_and_are_recorded(self, store, live_dialogs):
        result = _call("conversations_list", _agent(), {})
        assert result["success"] is True
        assert result["source"] == "live"
        assert result["count"] == 2
        refs = [row["ref"] for row in result["conversations"]]
        assert all(ref.startswith("conv_") for ref in refs)
        by_name = {row["name"]: row for row in result["conversations"]}
        assert by_name["Testing Agents"]["type"] == "group"
        assert by_name["Ann"]["type"] == "dm"
        # The listing recorded the addresses durably.
        assert store.lookup("telegram", "-10042")["name"] == "Testing Agents"

    def test_cached_listing_without_a_live_client(self, store, monkeypatch):
        import tools.conversation_tools as tools_mod

        monkeypatch.setattr(tools_mod, "_live_dialogs", lambda *a: None)
        monkeypatch.setattr(tools_mod, "_telegram_context", lambda: (None, None, "no telegram here"))
        store.record_conversation("telegram", "100", type="dm", name="Ann")
        result = _call("conversations_list", _agent(), {})
        assert result["success"] is True
        assert result["source"] == "cached"
        assert result["count"] == 1
        assert result["conversations"][0]["name"] == "Ann"

    def test_search_filters_by_name(self, store, live_dialogs):
        result = _call("conversations_list", _agent(), {"search": "ann"})
        assert result["count"] == 1
        assert result["conversations"][0]["name"] == "Ann"

    def test_non_telegram_platform_fails_closed(self, store):
        result = _call("conversations_list", _agent(), {"platform": "slack"})
        assert result["success"] is False


class TestConversationsTurn:
    @pytest.fixture
    def ref(self, store):
        return store.record_conversation("telegram", "-10042", type="group", name="Testing Agents")

    @pytest.fixture
    def stub_send(self, monkeypatch):
        import tools.conversation_tools as tools_mod

        monkeypatch.setattr(tools_mod, "_telegram_context", lambda: ("-10042", "100", None))
        sent = {"id": 555}
        monkeypatch.setattr(tools_mod, "_send_to_chat", lambda chat_id, message: (sent["id"], None))
        return sent

    def test_turn_returns_the_correlated_reply(self, store, ref, stub_send, monkeypatch):
        import tools.conversation_tools as tools_mod

        monkeypatch.setattr(tools_mod, "_telegram_context", lambda: ("-10042", "100", None))
        home = str(__import__("hermes_constants").get_hermes_home())

        def _reply():
            notify_inbound(home, "telegram", "-10042", message_id=777,
                           sender_id="100", text="hello back", reply_to_msg_id=555)

        threading.Timer(0.2, _reply).start()
        result = _call("conversations_turn", _agent(),
                       {"conversation_ref": ref, "message": "are you there?"})
        assert result["success"] is True
        assert result["status"] == "replied"
        assert result["sent_message_id"] == 555
        assert result["reply"]["message_id"] == 777
        assert result["reply"]["text"] == "hello back"

    def test_turn_timeout_is_a_timeout_not_a_receipt(self, store, ref, stub_send):
        result = _call("conversations_turn", _agent(),
                       {"conversation_ref": ref, "message": "are you there?",
                        "timeoutSeconds": 1})
        assert result["success"] is True
        assert result["status"] == "timeout"
        assert result["sent_message_id"] == 555
        assert "reply" not in result

    def test_unknown_ref_fails_closed(self, store, stub_send):
        result = _call("conversations_turn", _agent(),
                       {"conversation_ref": "conv_" + "0" * 32, "message": "hi"})
        assert result["success"] is False

    def test_send_failure_reports_the_error(self, store, ref, monkeypatch):
        import tools.conversation_tools as tools_mod

        monkeypatch.setattr(tools_mod, "_telegram_context", lambda: ("-10042", "100", None))
        monkeypatch.setattr(tools_mod, "_send_to_chat",
                            lambda chat_id, message: (None, "Telegram send failed: boom"))
        result = _call("conversations_turn", _agent(),
                       {"conversation_ref": ref, "message": "hi"})
        assert result["success"] is False
        assert "boom" in result["error"]

    def test_empty_message_fails_closed(self, store, ref, stub_send):
        result = _call("conversations_turn", _agent(),
                       {"conversation_ref": ref, "message": "   "})
        assert result["success"] is False
