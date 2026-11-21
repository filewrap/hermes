"""Tests for the Telegram Business Mode DM UI (/biz commands + hm:biz: cards).

Fake adapter + fake manager, both constructor-injected: no network, no
real Telegram client, no BusinessStateDB. The fake manager mirrors the
v2 manager's callback contract (direct send: card actions return their
name, mute/unmute apply and return "recent", trigger_word/persona arm
the owner-input capture, non-owner taps are refused).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from plugins.platforms.telegram.business_ui import TelegramBusinessUI


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAdapter:
    """Records rich-UI sends and MTProto edit requests; never touches Telegram."""

    def __init__(self, owner_id: str = "100"):
        self.owner_id = str(owner_id)
        self.sent: list[dict] = []
        self.requests: list = []
        self.edit_error = None
        self._next_id = 700
        self.gateway_runner = SimpleNamespace(
            _resolve_profile_home_for_source=lambda source: str(source.home),
        )

    def _is_owner_user(self, user_id, source=None) -> bool:
        return str(user_id) == self.owner_id

    def _authorized(self, source) -> bool:
        return True

    async def _send_rich_ui(self, source, text, markup=None, reply_to=None):
        self._next_id += 1
        self.sent.append(
            {"source": source, "text": text, "markup": markup,
             "reply_to": reply_to, "id": self._next_id},
        )
        return SimpleNamespace(id=self._next_id)

    def _peer_for(self, chat_id):
        return SimpleNamespace(chat_id=str(chat_id))

    async def _mtproto_rich_request(self, request):
        if self.edit_error is not None:
            raise self.edit_error
        self.requests.append(request)
        return SimpleNamespace(id=getattr(request, "id", 500))


class FakeCallbackEvent:
    def __init__(self, message_id: int = 500, actor_id: int = 100):
        self.query = SimpleNamespace(msg_id=message_id, user_id=actor_id)
        self.message_id = message_id
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text or "", bool(alert)))


class FakeManager:
    """In-memory stand-in for BusinessModeManager (v2 contract)."""

    CARD_ACTIONS = {"pause", "resume", "trigger", "trigger_word", "persona",
                    "mute", "unmute", "muted", "recent", "status", "connections"}

    def __init__(self):
        now = time.time()
        self.connections = [
            {
                "connection_id": "conn1", "owner_user_id": "100",
                "owner_chat_id": "100", "can_reply": True, "is_enabled": True,
                "auto_draft": True, "paused_chats": [],
                "rights": {"reply": True, "read_messages": True},
                "created_at": now - 3600, "updated_at": now - 60,
            },
            {
                "connection_id": "conn2", "owner_user_id": "100",
                "owner_chat_id": "100", "can_reply": False, "is_enabled": False,
                "auto_draft": True, "paused_chats": ["300"],
                "rights": {},
                "created_at": now - 7200, "updated_at": now - 3600,
            },
        ]
        self.recent = [
            {
                "connection_id": "conn1", "customer_chat_id": "200",
                "customer_msg_id": "5", "customer_name": "Alice",
                "text": "Hi, do you have this in stock?",
                "direction": "in", "created_at": now - 300,
            },
            {
                "connection_id": "conn1", "customer_chat_id": "201",
                "customer_msg_id": "9", "customer_name": "Bob",
                "text": "What time do you close today?",
                "direction": "in", "created_at": now - 60,
            },
            {
                "connection_id": "conn1", "customer_chat_id": "200",
                "customer_msg_id": None, "customer_name": "Alice",
                "text": "Yes, we do — want me to hold one for you?",
                "direction": "out", "created_at": now - 30,
            },
        ]
        self._settings = {
            "trigger_mode": "direct", "trigger_word": "",
            "persona": "Be friendly and concise.",
            "debounce_seconds": 8.0,
            "max_customer_text_chars": 4000,
        }
        self.callback_calls: list[str] = []
        self._pending_owner_input: dict[str, str] = {}

    def list_connections(self, *, owner_user_id=None, enabled_only=True):
        rows = [dict(c) for c in self.connections]
        if owner_user_id is not None:
            rows = [r for r in rows if r["owner_user_id"] == str(owner_user_id)]
        if enabled_only:
            rows = [r for r in rows if r["is_enabled"]]
        return rows

    def purge_dead_connections(self, owner_user_id):
        """Mirror the manager rule: with a live connection, drop the dead ones."""
        live = any(c["is_enabled"] and str(c["owner_user_id"]) == str(owner_user_id)
                   for c in self.connections)
        if not live:
            return 0
        before = len(self.connections)
        self.connections = [c for c in self.connections
                            if c["is_enabled"]
                            or str(c["owner_user_id"]) != str(owner_user_id)]
        return before - len(self.connections)

    def recent_messages(self, owner_chat_id, limit=10):
        conn_ids = {c["connection_id"] for c in self.connections
                    if c["owner_chat_id"] == str(owner_chat_id)}
        return [dict(r) for r in self.recent if r["connection_id"] in conn_ids][:limit]

    def set_auto_draft(self, connection_id, auto_draft):
        for conn in self.connections:
            if conn["connection_id"] == str(connection_id):
                conn["auto_draft"] = bool(auto_draft)

    def set_paused_chats(self, connection_id, paused_chats):
        for conn in self.connections:
            if conn["connection_id"] == str(connection_id):
                conn["paused_chats"] = [str(c) for c in paused_chats]

    def set_trigger(self, *, mode=None, word=None):
        if mode is not None:
            if mode not in {"direct", "trigger"}:
                raise ValueError(f"invalid business trigger mode: {mode!r}")
            self._settings["trigger_mode"] = mode
        if word is not None:
            cleaned = str(word).strip()
            if cleaned and any(ch.isspace() for ch in cleaned):
                raise ValueError("business trigger word must be a single word")
            self._settings["trigger_word"] = cleaned
        return {"trigger_mode": self._settings["trigger_mode"],
                "trigger_word": self._settings["trigger_word"]}

    def set_persona(self, persona):
        self._settings["persona"] = str(persona)
        return {"persona": self._settings["persona"]}

    def get_settings(self):
        return dict(self._settings)

    async def handle_callback(self, *, data, caller_user_id, answer):
        self.callback_calls.append(data)
        if not isinstance(data, str) or not data.startswith("hm:biz:"):
            return False
        rest = data[len("hm:biz:"):]
        parts = rest.split(":")
        if len(parts) == 1:
            if parts[0] in self.CARD_ACTIONS:
                if parts[0] in {"trigger_word", "persona"}:
                    self._pending_owner_input[str(caller_user_id)] = parts[0]
                return parts[0]
            await answer(text="Unknown action.")
            return True
        if len(parts) != 2:
            await answer(text="Invalid action.")
            return True
        action, chat_id = parts
        if action not in {"mute", "unmute"}:
            await answer(text="Unknown action.")
            return True
        row = next((r for r in self.recent
                    if str(r["customer_chat_id"]) == str(chat_id)), None)
        if row is None:
            await answer(text="Unknown customer chat.")
            return True
        conn = next((c for c in self.connections
                     if c["connection_id"] == row["connection_id"]), None)
        if conn is None or str(conn["owner_user_id"]) != str(caller_user_id):
            await answer(text="Unknown customer chat.")
            return True
        paused = list(conn.get("paused_chats") or [])
        if action == "mute":
            if chat_id not in paused:
                paused.append(chat_id)
        else:
            if chat_id in paused:
                paused.remove(chat_id)
        conn["paused_chats"] = paused
        return "recent"


def _source(home, *, user_id="100", chat_id="100"):
    return SimpleNamespace(
        home=home, user_id=str(user_id), chat_id=str(chat_id), chat_type="dm",
        thread_id=None, is_bot=False, username="owner", display_name="Owner",
    )


@pytest.fixture()
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    adapter = FakeAdapter()
    manager = FakeManager()
    ui = TelegramBusinessUI(adapter, manager_factory=lambda _home: manager)
    return SimpleNamespace(home=home, adapter=adapter, manager=manager, ui=ui)


def _last_text(adapter):
    assert adapter.sent, "expected the UI to send a rich card"
    return adapter.sent[-1]["text"]


# ---------------------------------------------------------------------------
# /biz status dashboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_biz_renders_rich_status_card_with_connections(env):
    handled = await env.ui.handle_command(None, _source(env.home), "biz", "")
    assert handled is True
    text = _last_text(env.adapter)
    assert "<tg-button" in text
    assert "hm:biz:pause" in text
    assert "conn1" in text
    assert "conn2" not in text  # dead connection purged on render (design §5)
    assert "Alice" not in text  # status lists connections, not recent chats
    assert "direct" in text
    assert "badge:enabled" in text  # active state badge on the connection id
    assert "reply to customers" in text  # permissions list from rights
    assert "read messages" in text
    assert "messages: 2 in · 1 out" in text
    assert "Replies go straight to the customer" in text  # legend


@pytest.mark.asyncio
async def test_status_buttons_show_selected_states(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "") is True
    text = _last_text(env.adapter)
    # Replying is on: its button carries the selected (success) style.
    assert 'style="success" data="hm:biz:pause"' in text
    # The Status view itself is selected; Recent/Connections are not.
    assert 'style="success" data="hm:biz:status"' in text
    assert "Trigger: direct" in text
    assert "Muted chats (0)" in text
    assert "Persona: Be friendly" in text


@pytest.mark.asyncio
async def test_status_trigger_button_shows_current_word(env):
    env.manager.set_trigger(mode="trigger", word="hello")
    assert await env.ui.handle_command(None, _source(env.home), "biz", "") is True
    text = _last_text(env.adapter)
    assert "Trigger: hello" in text
    assert 'style="success" data="hm:biz:trigger"' in text


@pytest.mark.asyncio
async def test_dead_connection_purged_on_status_render(env):
    assert any(c["connection_id"] == "conn2" for c in env.manager.connections)
    assert await env.ui.handle_command(None, _source(env.home), "biz", "") is True
    assert all(c["connection_id"] != "conn2" for c in env.manager.connections)
    assert "conn2" not in _last_text(env.adapter)


@pytest.mark.asyncio
async def test_biz_no_connections_renders_setup_card(env):
    env.manager.connections = []
    handled = await env.ui.handle_command(None, _source(env.home), "biz", "")
    assert handled is True
    text = _last_text(env.adapter)
    assert "Business" in text
    assert "Chatbots" in text
    assert "conn1" not in text


@pytest.mark.asyncio
async def test_biz_non_owner_is_refused(env):
    handled = await env.ui.handle_command(
        None, _source(env.home, user_id="999", chat_id="999"), "biz", "")
    assert handled is True
    text = _last_text(env.adapter)
    assert "Owner only" in text
    assert env.manager.callback_calls == []


@pytest.mark.asyncio
async def test_handle_command_ignores_other_commands(env):
    assert await env.ui.handle_command(None, _source(env.home), "managedbots", "") is False
    assert env.adapter.sent == []


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_turns_auto_draft_off_for_active_connections(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "pause") is True
    active = [c for c in env.manager.connections if c["is_enabled"]]
    assert all(c["auto_draft"] is False for c in active)
    assert "paused" in _last_text(env.adapter).lower()


@pytest.mark.asyncio
async def test_resume_turns_auto_draft_on(env):
    env.manager.connections[0]["auto_draft"] = False
    assert await env.ui.handle_command(None, _source(env.home), "biz", "resume") is True
    assert env.manager.connections[0]["auto_draft"] is True
    assert "resumed" in _last_text(env.adapter).lower()


@pytest.mark.asyncio
async def test_pause_with_no_connections_says_so(env):
    env.manager.connections = []
    assert await env.ui.handle_command(None, _source(env.home), "biz", "pause") is True
    assert "No active connections" in _last_text(env.adapter)


# ---------------------------------------------------------------------------
# off / on (mute)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_off_mutes_chat_by_id(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "off 200") is True
    assert "200" in env.manager.connections[0]["paused_chats"]
    assert "muted" in _last_text(env.adapter).lower()


@pytest.mark.asyncio
async def test_on_unmutes_chat(env):
    env.manager.connections[0]["paused_chats"] = ["200"]
    assert await env.ui.handle_command(None, _source(env.home), "biz", "on 200") is True
    assert "200" not in env.manager.connections[0]["paused_chats"]
    assert "unmuted" in _last_text(env.adapter).lower()


@pytest.mark.asyncio
async def test_off_replying_to_recent_card_mutes_that_chat(env):
    # Recent row for chat 200 exists; replying to the recent card mutes it.
    # (The v2 UI no longer carries a draft card to reply to.)
    assert await env.ui.handle_command(
        None, _source(env.home), "biz", "off 200") is True
    assert "200" in env.manager.connections[0]["paused_chats"]


@pytest.mark.asyncio
async def test_off_unknown_chat_shows_usage_error(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "off 999") is True
    text = _last_text(env.adapter)
    assert "999" in text
    assert "/biz off" in text


@pytest.mark.asyncio
async def test_off_with_no_id_and_no_reply_shows_usage(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "off") is True
    assert "/biz off" in _last_text(env.adapter)


# ---------------------------------------------------------------------------
# recent / connections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drafts_command_is_unknown_now(env):
    """``/biz drafts`` is gone with the drafts — it shows the usage card."""
    assert await env.ui.handle_command(None, _source(env.home), "biz", "drafts") is True
    text = _last_text(env.adapter)
    assert "Unknown business command" in text
    assert "hm:biz:drafts" not in text


@pytest.mark.asyncio
async def test_recent_lists_messages(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "recent") is True
    text = _last_text(env.adapter)
    assert "Alice" in text
    assert "in stock" in text
    assert "Bob" in text
    assert "badge:chat" in text  # every Recent row carries the chat badge


@pytest.mark.asyncio
async def test_connections_lists(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "connections") is True
    text = _last_text(env.adapter)
    assert "conn1" in text
    assert "conn2" not in text  # dead connection purged on render (design §5)
    assert "send on" in text.lower() or "send: on" in text.lower()
    assert "badge:enabled" in text  # active state badge on the connection id
    assert "reply to customers" in text


# ---------------------------------------------------------------------------
# trigger / mode / persona
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_show_reports_current(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "trigger") is True
    text = _last_text(env.adapter)
    assert "direct" in text


@pytest.mark.asyncio
async def test_trigger_set_reports_and_applies(env):
    assert await env.ui.handle_command(
        None, _source(env.home), "biz", "trigger hello") is True
    assert env.manager.get_settings()["trigger_word"] == "hello"
    text = _last_text(env.adapter)
    assert "hello" in text
    assert "Saved." in text  # the config helper persists the change now


@pytest.mark.asyncio
async def test_trigger_off_clears_word(env):
    env.manager.set_trigger(word="hello")
    assert await env.ui.handle_command(
        None, _source(env.home), "biz", "trigger off") is True
    assert env.manager.get_settings()["trigger_word"] == ""


@pytest.mark.asyncio
async def test_mode_show_and_set(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "mode") is True
    assert "direct" in _last_text(env.adapter)
    assert await env.ui.handle_command(
        None, _source(env.home), "biz", "mode trigger") is True
    assert env.manager.get_settings()["trigger_mode"] == "trigger"
    assert "trigger" in _last_text(env.adapter)


@pytest.mark.asyncio
async def test_mode_invalid_word_shows_usage(env):
    assert await env.ui.handle_command(
        None, _source(env.home), "biz", "mode banana") is True
    text = _last_text(env.adapter)
    assert "direct" in text and "trigger" in text
    assert env.manager.get_settings()["trigger_mode"] == "direct"


@pytest.mark.asyncio
async def test_persona_show_and_set(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "persona") is True
    assert "friendly" in _last_text(env.adapter).lower()
    assert await env.ui.handle_command(
        None, _source(env.home), "biz", "persona Be terse.") is True
    assert env.manager.get_settings()["persona"] == "Be terse."
    assert "Be terse." in _last_text(env.adapter)


# ---------------------------------------------------------------------------
# send / discard by id — gone in v2 (replies are direct, no approval step)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_and_discard_commands_are_unknown_now(env):
    """``/biz send`` / ``/biz discard`` no longer exist — usage card, no call."""
    for subcommand in ("send 1", "discard 2", "send", "discard"):
        assert await env.ui.handle_command(None, _source(env.home), "biz", subcommand) is True
        assert "Unknown business command" in _last_text(env.adapter)
    assert env.manager.callback_calls == []


# ---------------------------------------------------------------------------
# help / unknown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_lists_commands(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "help") is True
    text = _last_text(env.adapter)
    for command in ("/biz pause", "/biz recent", "/biz connections",
                    "/biz trigger", "/biz mode", "/biz persona", "/biz help"):
        assert command in text


@pytest.mark.asyncio
async def test_unknown_subcommand_shows_usage(env):
    assert await env.ui.handle_command(None, _source(env.home), "biz", "frobnicate") is True
    text = _last_text(env.adapter)
    assert "frobnicate" in text
    assert "/biz help" in text


# ---------------------------------------------------------------------------
# callbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_mute_delegates_to_manager(env):
    event = FakeCallbackEvent(message_id=500, actor_id=100)
    handled = await env.ui.handle_callback(event, _source(env.home), "hm:biz:mute:200")
    assert handled is True
    assert env.manager.callback_calls == ["hm:biz:mute:200"]
    assert "200" in env.manager.connections[0]["paused_chats"]
    assert env.adapter.requests, "expected the recent card to be edited in place"
    assert type(env.adapter.requests[-1]).__name__ == "EditMessageRequest"
    assert event.answers == [("Updated", False)]


@pytest.mark.asyncio
async def test_callback_trigger_word_arms_owner_input(env):
    event = FakeCallbackEvent(message_id=500, actor_id=100)
    assert await env.ui.handle_callback(event, _source(env.home), "hm:biz:trigger_word") is True
    assert env.manager._pending_owner_input.get("100") == "trigger_word"
    request = env.adapter.requests[-1]
    assert type(request).__name__ == "EditMessageRequest"
    assert "trigger word" in request.message.lower()


@pytest.mark.asyncio
async def test_callback_non_owner_gets_alert_and_no_manager_call(env):
    event = FakeCallbackEvent(message_id=500, actor_id=999)
    handled = await env.ui.handle_callback(
        event, _source(env.home, user_id="999", chat_id="999"), "hm:biz:mute:200")
    assert handled is True
    assert env.manager.callback_calls == []
    assert "200" not in env.manager.connections[0]["paused_chats"]
    assert event.answers and event.answers[0][1] is True  # alert, not toast


@pytest.mark.asyncio
async def test_callback_status_edits_card_in_place(env):
    event = FakeCallbackEvent(message_id=500, actor_id=100)
    assert await env.ui.handle_callback(event, _source(env.home), "hm:biz:status") is True
    assert env.adapter.requests, "expected the status card to be edited in place"
    request = env.adapter.requests[-1]
    assert type(request).__name__ == "EditMessageRequest"
    assert env.adapter.sent == [], "a status tap must not send a fresh card"
    assert event.answers == [("Updated", False)]


@pytest.mark.asyncio
async def test_callback_status_unchanged_card_sends_no_fresh_card(env):
    # Tapping Status while already viewing status rebuilds byte-identical
    # html, so Telegram refuses the no-op edit with MessageNotModifiedError.
    # The card already shows the current state: answer the toast and send
    # nothing — never a fresh duplicate card.
    from telethon.errors import MessageNotModifiedError

    env.adapter.edit_error = MessageNotModifiedError(None)
    event = FakeCallbackEvent(message_id=500, actor_id=100)
    assert await env.ui.handle_callback(event, _source(env.home), "hm:biz:status") is True
    assert env.adapter.sent == [], "an unchanged card must not be re-sent"
    assert event.answers == [("Updated", False)]


@pytest.mark.asyncio
async def test_callback_pause_mutates_and_rerenders_card(env):
    event = FakeCallbackEvent(message_id=500, actor_id=100)
    assert await env.ui.handle_callback(event, _source(env.home), "hm:biz:pause") is True
    assert env.manager.connections[0]["auto_draft"] is False
    assert env.adapter.requests, "expected the status card to be edited in place"
    request = env.adapter.requests[-1]
    assert type(request).__name__ == "EditMessageRequest"
    assert "paused" in request.message.lower()
    assert event.answers == [("Replying paused", False)]


@pytest.mark.asyncio
async def test_callback_trigger_toggles_mode(env):
    event = FakeCallbackEvent(message_id=500, actor_id=100)
    assert await env.ui.handle_callback(event, _source(env.home), "hm:biz:trigger") is True
    assert env.manager.get_settings()["trigger_mode"] == "trigger"
    request = env.adapter.requests[-1]
    assert type(request).__name__ == "EditMessageRequest"
    assert "trigger" in request.message.lower()
    assert event.answers == [("Trigger mode: trigger", False)]


@pytest.mark.asyncio
async def test_callback_open_is_unknown_now(env):
    # The draft route is gone in v2 — hm:biz:open:<id> answers "Unknown action."
    event = FakeCallbackEvent(message_id=500, actor_id=100)
    assert await env.ui.handle_callback(event, _source(env.home), "hm:biz:open:1") is True
    assert ("Unknown action.", False) in event.answers
    assert env.adapter.sent == []


@pytest.mark.asyncio
async def test_callback_non_biz_data_returns_false(env):
    event = FakeCallbackEvent()
    assert await env.ui.handle_callback(event, _source(env.home), "tgb:abc") is False
    assert await env.ui.handle_callback(event, _source(env.home), "hm:other:x") is False
    assert event.answers == []
