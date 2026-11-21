"""Behaviour contracts for the telegram_business tool.

The owner manages Business Mode from plain words; the tool reads and mutates
the SAME store the manager owns (``<profile-home>/telegram-business/state.db``)
and the SAME config the UI worker persists (``platforms.telegram.business``).
Dispatch goes through the real registry (``handle_function_call``), not the
bare handler. Tests run against a temp HERMES_HOME; no live Telegram, no LLM
calls. Delivery goes through the real ``send_business_message`` wrapper
against a fake client that records the TL request.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.functions import InvokeWithBusinessConnectionRequest
from telethon.tl.types import InputPeerUser

from gateway.session_context import clear_session_vars, set_session_vars
from model_tools import handle_function_call
from plugins.platforms.telegram.business_state import BusinessStateDB

# Imported for its registration side effect.
import tools.telegram_business_tool  # noqa: F401

OWNER_ID = "100"
OTHER_ID = "777"
CONNECTION_ID = "conn-1"
CUSTOMER_ID = "555"


@pytest.fixture
def home_env(tmp_path, monkeypatch):
    """Temp Hermes home: Path.home and HERMES_HOME both point inside tmp."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def dm_session(home_env):
    tokens = set_session_vars(
        platform="telegram", chat_id=OWNER_ID, chat_type="dm", user_id=OWNER_ID)
    try:
        yield home_env
    finally:
        clear_session_vars(tokens)


def _seed(home, *, connection=CONNECTION_ID, owner=OWNER_ID,
          customer=CUSTOMER_ID):
    db = BusinessStateDB(home / "telegram-business" / "state.db")
    try:
        db.upsert_telegram_business_connection(
            connection_id=connection, owner_user_id=owner, owner_chat_id=owner,
            can_reply=True, is_enabled=True)
        db.record_business_message(
            connection, customer, "1", "Cust", "Hi there", "in")
    finally:
        db.close()


def _db(home):
    return BusinessStateDB(home / "telegram-business" / "state.db")


def _owner_agent(platform="telegram"):
    # A Telegram turn trusts the adapter's catalog filter for the owner
    # gate; the handler re-check passes here by design.
    return SimpleNamespace(platform=platform)


def _call(args, agent=None):
    return json.loads(handle_function_call(
        "telegram_business", args,
        parent_agent=agent if agent is not None else _owner_agent()))


def test_status_reports_connections_and_settings(dm_session):
    _seed(dm_session)
    result = _call({"action": "status"})
    assert "pending_drafts" not in result
    assert len(result["connections"]) == 1
    assert result["connections"][0]["connection_id"] == CONNECTION_ID
    assert result["connections"][0]["auto_draft"] is True
    assert result["settings"]["trigger_mode"] == "direct"
    assert result["settings"]["enabled"] is True


def test_drafts_action_is_unknown_now(dm_session):
    """The drafts action is gone in v2 — replies are direct, no approval step."""
    _seed(dm_session)
    result = _call({"action": "drafts"})
    assert "error" in result
    assert "Unknown business action" in result["error"]


def test_recent_lists_customer_messages(dm_session):
    _seed(dm_session)
    result = _call({"action": "recent"})
    assert len(result["messages"]) == 1
    assert result["messages"][0]["text"] == "Hi there"
    assert result["messages"][0]["direction"] == "in"


def test_connections_lists_rows(dm_session):
    _seed(dm_session)
    result = _call({"action": "connections"})
    assert len(result["connections"]) == 1
    assert result["connections"][0]["can_reply"] is True
    assert result["connections"][0]["is_enabled"] is True


class _FakeClient:
    """Records every TL request; never touches the network."""

    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=1001)


@pytest.fixture
def live_adapter(monkeypatch):
    """Fake connected adapter behind the tool's live-adapter seam."""
    client = _FakeClient()
    adapter = SimpleNamespace(_client=client)

    async def _input_peer_for(chat_id):
        return InputPeerUser(int(chat_id), 0)

    adapter._input_peer_for = _input_peer_for

    async def _fake_dispatch(runner, factory, message):
        return await factory()

    monkeypatch.setattr(
        "tools.send_message_senders._live_adapter",
        lambda platform: (object(), adapter))
    monkeypatch.setattr(
        "tools.send_message_tool._dispatch_on_gateway_loop", _fake_dispatch)
    monkeypatch.setattr("model_tools._run_async", lambda coro: asyncio.run(coro))
    return adapter


@pytest.fixture
def no_adapter(monkeypatch):
    """No live adapter: delivery is refused and nothing is recorded."""
    monkeypatch.setattr(
        "tools.send_message_senders._live_adapter", lambda platform: (None, None))
    return None


def test_send_records_the_out_message_with_customer_name(dm_session, live_adapter):
    _seed(dm_session)
    result = _call({"action": "send", "connection_id": CONNECTION_ID,
                    "customer_chat_id": CUSTOMER_ID, "text": "Hello!"})
    assert result["status"] == "sent"
    sent = [r for r in live_adapter._client.requests
            if isinstance(r, InvokeWithBusinessConnectionRequest)]
    assert len(sent) == 1
    assert sent[0].connection_id == CONNECTION_ID
    assert sent[0].query.message == "Hello!"
    db = _db(dm_session)
    try:
        rows = db.list_recent_business_messages(OWNER_ID, limit=10)
    finally:
        db.close()
    out = [r for r in rows if r["direction"] == "out"]
    assert len(out) == 1
    assert out[0]["text"] == "Hello!"
    assert out[0]["customer_name"] == "Cust"


@pytest.fixture
def biz_session(home_env):
    """A business reply turn: thread id starts with ``biz-``."""
    tokens = set_session_vars(
        platform="telegram", chat_id=OWNER_ID, chat_type="dm", user_id=OWNER_ID,
        thread_id="biz-conn-1-555")
    try:
        yield home_env
    finally:
        clear_session_vars(tokens)


def test_send_refuses_inside_a_business_reply_turn(biz_session, live_adapter):
    _seed(biz_session)
    result = _call({"action": "send", "connection_id": CONNECTION_ID,
                    "customer_chat_id": CUSTOMER_ID, "text": "Hello!"})
    assert "error" in result
    assert "business reply turn" in result["error"]
    assert live_adapter._client.requests == []


def test_send_with_explicit_text_delivers_via_live_adapter(dm_session, live_adapter):
    _seed(dm_session)
    result = _call({"action": "send", "connection_id": CONNECTION_ID,
                    "customer_chat_id": CUSTOMER_ID, "text": "Hello!"})
    assert result["status"] == "sent"
    assert result["connection_id"] == CONNECTION_ID
    sent = [r for r in live_adapter._client.requests
            if isinstance(r, InvokeWithBusinessConnectionRequest)]
    assert len(sent) == 1
    assert sent[0].query.message == "Hello!"


def test_send_with_explicit_text_without_adapter_errors(dm_session, no_adapter):
    _seed(dm_session)
    result = _call({"action": "send", "connection_id": CONNECTION_ID,
                    "customer_chat_id": CUSTOMER_ID, "text": "Hello!"})
    assert "error" in result
    assert "unavailable" in result["error"].lower()


def test_discard_action_is_unknown_now(dm_session):
    """The discard action is gone in v2 — there are no drafts to discard."""
    _seed(dm_session)
    result = _call({"action": "discard"})
    assert "error" in result
    assert "Unknown business action" in result["error"]


def test_pause_and_resume_flip_auto_draft(dm_session):
    _seed(dm_session)
    assert _call({"action": "pause"})["auto_draft"] is False
    db = _db(dm_session)
    try:
        assert db.get_telegram_business_connection(CONNECTION_ID)["auto_draft"] is False
    finally:
        db.close()
    assert _call({"action": "resume"})["auto_draft"] is True
    db = _db(dm_session)
    try:
        assert db.get_telegram_business_connection(CONNECTION_ID)["auto_draft"] is True
    finally:
        db.close()


def test_mute_and_unmute_one_customer_chat(dm_session):
    _seed(dm_session)
    result = _call({"action": "mute", "customer_chat_id": CUSTOMER_ID})
    assert result["muted"] is True
    assert result["customer_chat_id"] == CUSTOMER_ID
    db = _db(dm_session)
    try:
        paused = db.get_telegram_business_connection(CONNECTION_ID)["paused_chats"]
    finally:
        db.close()
    assert CUSTOMER_ID in paused
    assert _call({"action": "unmute", "customer_chat_id": CUSTOMER_ID})["muted"] is False
    db = _db(dm_session)
    try:
        paused = db.get_telegram_business_connection(CONNECTION_ID)["paused_chats"]
    finally:
        db.close()
    assert CUSTOMER_ID not in paused


def test_trigger_show_and_set_persists_to_the_config_store(dm_session):
    from plugins.platforms.telegram.business_config import load_business_settings

    shown = _call({"action": "trigger"})
    assert shown["trigger_mode"] == "direct"
    assert shown["trigger_word"] == ""
    result = _call({"action": "trigger", "trigger_mode": "trigger",
                    "trigger_word": "hello"})
    assert result["trigger_mode"] == "trigger"
    assert result["trigger_word"] == "hello"
    assert result["changed"] == ["trigger_mode", "trigger_word"]
    stored = load_business_settings(dm_session)
    assert stored["trigger_mode"] == "trigger"
    assert stored["trigger_word"] == "hello"


def test_persona_show_and_set_persists_to_the_config_store(dm_session):
    from plugins.platforms.telegram.business_config import load_business_settings

    shown = _call({"action": "persona"})
    assert shown["owner_persona"].startswith("You are replying")
    result = _call({"action": "persona", "persona": "Reply briefly, in Hindi."})
    assert result["owner_persona"] == "Reply briefly, in Hindi."
    assert load_business_settings(dm_session)["owner_persona"] == "Reply briefly, in Hindi."


def test_invalid_trigger_word_is_refused_and_nothing_is_stored(dm_session):
    from plugins.platforms.telegram.business_config import load_business_settings

    assert "error" in _call({"action": "trigger", "trigger_word": "two words"})
    assert "error" in _call({"action": "trigger", "trigger_mode": "shout"})
    stored = load_business_settings(dm_session)
    assert stored["trigger_mode"] == "direct"
    assert stored["trigger_word"] == ""


def test_unknown_action_errors_cleanly(dm_session):
    result = _call({"action": "explode"})
    assert "error" in result
    assert "explode" in result["error"]


def test_tenant_scope_denied_and_nothing_changes(dm_session):
    from agent.tenant_scope import TenantScope, reset_tenant_scope, set_tenant_scope

    _seed(dm_session)
    scope = TenantScope(dm_session, object(), "room-a", dm_session / "a", "tg-a")
    token = set_tenant_scope(scope)
    try:
        result = _call({"action": "pause"})
    finally:
        reset_tenant_scope(token)
    assert "error" in result
    assert "not available in isolated tenant sessions" in result["error"]
    db = _db(dm_session)
    try:
        assert db.get_telegram_business_connection(CONNECTION_ID)["auto_draft"] is True
    finally:
        db.close()


def test_dm_non_owner_denied(dm_session):
    from gateway.telegram_personal_settings import (
        reset_personal_memory_context,
        set_personal_memory_context,
    )

    _seed(dm_session)
    (dm_session / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
    agent = SimpleNamespace(platform="telegram")
    token = set_personal_memory_context(dm_session, OTHER_ID)
    try:
        result = _call({"action": "mute", "customer_chat_id": CUSTOMER_ID}, agent=agent)
    finally:
        reset_personal_memory_context(token)
    assert "error" in result
    assert "Only the owner" in result["error"]
    db = _db(dm_session)
    try:
        paused = db.get_telegram_business_connection(CONNECTION_ID)["paused_chats"]
    finally:
        db.close()
    assert CUSTOMER_ID not in paused


def test_tool_is_registered_in_the_telegram_admin_toolset():
    from toolsets import TOOLSETS
    from tools.registry import registry

    entry = registry.get_entry("telegram_business")
    assert entry is not None
    assert entry.toolset == "telegram_admin"
    assert "telegram_business" in TOOLSETS["telegram_admin"]["tools"]
    assert "telegram_business" in TOOLSETS["hermes-telegram"]["tools"]
