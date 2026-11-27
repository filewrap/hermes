"""Adapter wiring for Telegram Business Mode (chat automation).

Covers the Wave-2 adapter seam against the live ``TelethonAdapter``:
Raw-handler registration for the five business updates, intake through
the real manager (debounce 0) with a fake turn, the owner/bot-sent
guard, the reply-to-bot trigger kwarg, ``reply_via_turn`` (owner
user_id + owner profile pin, final text IS the reply), the
``InvokeWithBusinessConnection`` send shape, the ``hm:biz:`` callback
gate + dispatch, the DM owner-input intercept, the guest-query routing
that keeps business messages out of the guest path, and ``/biz``
routing. No network: the client is a fake that records every TL
request.
"""

from __future__ import annotations

import asyncio
import json
import sys
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import events as tl_events
from telethon.tl import types as tl_types
from telethon.tl.functions import InvokeWithBusinessConnectionRequest
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.types import (
    BotBusinessConnection,
    BusinessBotRights,
    InputPeerUser,
    Message,
    PeerUser,
)

from gateway.config import PlatformConfig
from plugins.platforms.telegram import business_adapter
from plugins.platforms.telegram.business_manager import BusinessModeManager
from plugins.platforms.telegram.business_state import BusinessStateDB
from plugins.platforms.telegram.operations_routing import TELEGRAM_OPERATION_COMMANDS
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

OWNER_ID = 42
CUSTOMER_ID = 200
CUSTOMER_CHAT = 200
CONN_ID = "conn-1"
BOT_ID = 999999


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records every TL request; never touches the network."""

    def __init__(self):
        self.requests = []
        self.fail_with = None

    async def __call__(self, request):
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(id=1001)


class _RecordingClient:
    """Records ``add_event_handler`` calls for the registration test."""

    def __init__(self):
        self.handlers = []

    def add_event_handler(self, handler, event=None):
        self.handlers.append((handler, event))


def _adapter(tmp_path):
    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="x"))
    adapter._client = _FakeClient()
    adapter._telegram_transport_home = tmp_path
    adapter._bot_id = BOT_ID
    return adapter


def _seed_manager(adapter, tmp_path, *, debounce_seconds=0.0,
                  trigger_mode="direct", trigger_word=""):
    """A real manager wired to the adapter's injected callables."""
    db = BusinessStateDB(tmp_path / "biz_state.db")
    manager = BusinessModeManager(
        db,
        send_message=partial(business_adapter.send_owner_message, adapter),
        reply_via_turn=partial(business_adapter.reply_via_turn, adapter),
        send_business_message=partial(business_adapter.send_business_message, adapter),
        debounce_seconds=debounce_seconds,
        trigger_mode=trigger_mode,
        trigger_word=trigger_word,
    )
    adapter._telegram_business_manager = manager
    adapter._telegram_business_manager_home = adapter._telegram_transport_home
    return manager


def _fake_turn(adapter, text="Reply text"):
    """Install a fake ``_message_handler``; return the received events."""
    calls = []

    async def handler(event_obj):
        calls.append(event_obj)
        return SimpleNamespace(text=text)

    adapter._message_handler = handler
    return calls


def _tl_connection(*, conn_id=CONN_ID, owner_id=OWNER_ID, enabled=True, reply=True):
    return BotBusinessConnection(
        connection_id=conn_id,
        user_id=owner_id,
        dc_id=1,
        date=None,
        disabled=not enabled,
        rights=BusinessBotRights(reply=reply),
    )


def _tl_message(*, msg_id=1, customer_chat_id=CUSTOMER_CHAT, customer_id=CUSTOMER_ID,
                text="Hi there", via_business_bot_id=None, from_id=None, peer=None):
    return Message(
        id=msg_id,
        peer_id=peer if peer is not None else PeerUser(customer_chat_id),
        date=None,
        message=text,
        out=False,
        mentioned=False,
        media_unread=False,
        silent=False,
        post=False,
        from_scheduled=False,
        legacy=False,
        edit_hide=False,
        pinned=False,
        noforwards=False,
        invert_media=False,
        offline=False,
        from_id=from_id if from_id is not None else PeerUser(customer_id),
        via_business_bot_id=via_business_bot_id,
    )


def _connect_update(**kwargs):
    return tl_types.UpdateBotBusinessConnect(connection=_tl_connection(**kwargs), qts=1)


def _new_message_update(*, text="Hi there", msg_id=1, **message_kwargs):
    return tl_types.UpdateBotNewBusinessMessage(
        connection_id=CONN_ID,
        message=_tl_message(text=text, msg_id=msg_id, **message_kwargs),
        qts=2,
    )


def _callback_event(*, data, sender_id=OWNER_ID):
    """Minimal Telethon CallbackQuery event (bytes data, DM with the bot)."""
    sender = SimpleNamespace(id=sender_id, first_name="Owner", last_name=None, bot=False)
    chat = SimpleNamespace(id=OWNER_ID, first_name="Owner", title=None)
    message = SimpleNamespace(id=101, chat_id=OWNER_ID, reply_to=None)
    event = SimpleNamespace(
        data=data,
        message_id=101,
        chat_id=OWNER_ID,
        sender_id=sender_id,
        sender=sender,
        chat=chat,
        message=message,
        get_sender=AsyncMock(return_value=sender),
        get_chat=AsyncMock(return_value=chat),
        answers=[],
    )

    async def _answer(*args, **kwargs):
        event.answers.append((args, kwargs))

    event.answer = _answer
    return event


def _dm_event(adapter, *, text, sender_id=OWNER_ID):
    """Minimal NewMessage event for an owner DM (drives _process_inbound_event)."""
    sender = SimpleNamespace(id=sender_id, first_name="Owner", last_name=None, bot=False)
    chat = SimpleNamespace(id=sender_id, first_name="Owner", title=None)
    message = _tl_message(msg_id=55, customer_chat_id=sender_id, customer_id=sender_id,
                          text=text, peer=PeerUser(sender_id),
                          from_id=PeerUser(sender_id))
    return SimpleNamespace(
        message=message,
        chat_id=sender_id,
        sender_id=sender_id,
        sender=sender,
        chat=chat,
        get_sender=AsyncMock(return_value=sender),
        get_chat=AsyncMock(return_value=chat),
    )


def _guest_query_update(*, chat_id=CUSTOMER_CHAT, sender_id=OWNER_ID,
                        text="@againemiliabot Say what's 2+2?"):
    """Minimal UpdateBotGuestChatQuery: owner @mentions the bot in a business chat."""
    message = _tl_message(
        msg_id=77, customer_chat_id=chat_id, customer_id=sender_id,
        text=text, peer=PeerUser(chat_id), from_id=PeerUser(sender_id))
    return SimpleNamespace(
        query_id=12345, message=message, reference_messages=[])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_raw_handlers_registered_for_all_five_business_updates(tmp_path):
    adapter = _adapter(tmp_path)
    recording = _RecordingClient()
    adapter._client = recording
    adapter._handlers_registered = False
    adapter._register_handlers(tl_events)
    registered = {getattr(event, "types", None)
                  for _, event in recording.handlers if event is not None}
    for update_type in (
        tl_types.UpdateBotBusinessConnect,
        tl_types.UpdateBotNewBusinessMessage,
        tl_types.UpdateBotEditBusinessMessage,
        tl_types.UpdateBotDeleteBusinessMessage,
        tl_types.UpdateBusinessBotCallbackQuery,
    ):
        assert update_type in registered


# ---------------------------------------------------------------------------
# Intake: connect + customer message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_update_records_connection_and_onboards_owner(tmp_path):
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    row = manager._db.get_telegram_business_connection(CONN_ID)
    assert row is not None
    assert row["is_enabled"] is True
    assert row["can_reply"] is True
    assert row["owner_chat_id"] == str(OWNER_ID)
    sends = [r for r in adapter._client.requests if isinstance(r, SendMessageRequest)]
    assert sends and "Business" in sends[0].message


@pytest.mark.asyncio
async def test_customer_message_replies_via_turn_and_direct_sends(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter, "Sure, happy to help!")
    await business_adapter.handle_business_connect(adapter, _connect_update())
    adapter._remember_peer(str(CUSTOMER_CHAT), InputPeerUser(CUSTOMER_CHAT, 0))
    adapter._client.requests.clear()
    await business_adapter.handle_business_new_message(
        adapter, _new_message_update(text="Hi, do you have this in stock?"))
    assert len(turn_calls) == 1
    event_obj = turn_calls[0]
    assert event_obj.source.thread_id == f"biz-{CONN_ID}-{CUSTOMER_CHAT}"
    # The turn runs as the OWNER, never as the customer.
    assert event_obj.source.user_id == str(OWNER_ID)
    assert event_obj.source.profile == "default"
    assert event_obj.source.chat_type == "dm"
    # The reply went DIRECTLY to the customer via the business connection.
    delivered = [r for r in adapter._client.requests
                 if isinstance(r, InvokeWithBusinessConnectionRequest)]
    assert len(delivered) == 1
    assert delivered[0].connection_id == CONN_ID
    assert delivered[0].query.message == "Sure, happy to help!"
    # The owner DM got only the minimal sent notice.
    sends = [r for r in adapter._client.requests if isinstance(r, SendMessageRequest)]
    assert len(sends) == 1
    assert "Sent to" in sends[0].message


@pytest.mark.asyncio
async def test_unknown_connection_business_message_is_ignored(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter)
    await business_adapter.handle_business_new_message(
        adapter, _new_message_update(text="hello?"))
    assert turn_calls == []
    assert adapter._client.requests == []


@pytest.mark.asyncio
async def test_malformed_business_updates_are_dropped_not_raised(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter)
    await business_adapter.handle_business_connect(adapter, SimpleNamespace())
    await business_adapter.handle_business_new_message(
        adapter, SimpleNamespace(connection_id=None, message=None))
    await business_adapter.handle_business_delete(
        adapter, SimpleNamespace(connection_id=None, peer=None, messages=None))
    await business_adapter.handle_business_callback_query(
        adapter, SimpleNamespace(connection_id="nope"))
    assert turn_calls == []
    assert adapter._client.requests == []


# ---------------------------------------------------------------------------
# Guard: owner-sent / bot-sent business messages start no turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_sent_business_message_starts_no_turn(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    before = len(adapter._client.requests)
    await business_adapter.handle_business_new_message(
        adapter, _new_message_update(text="dinner tonight?",
                                     from_id=PeerUser(OWNER_ID)))
    assert turn_calls == []
    assert len(adapter._client.requests) == before


@pytest.mark.asyncio
async def test_bot_sent_business_message_starts_no_turn(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    before = len(adapter._client.requests)
    await business_adapter.handle_business_new_message(
        adapter, _new_message_update(text="approved reply",
                                     via_business_bot_id=BOT_ID))
    assert turn_calls == []
    assert len(adapter._client.requests) == before


# ---------------------------------------------------------------------------
# Trigger kwarg passthrough (reply-to-bot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_to_business_bot_passes_trigger_kwarg(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path, trigger_mode="trigger", trigger_word="help")
    turn_calls = _fake_turn(adapter)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    # Plain text without the word and without a reply: no reply.
    await business_adapter.handle_business_new_message(
        adapter, _new_message_update(text="hello"))
    assert turn_calls == []
    # The word matches: replies.
    await business_adapter.handle_business_new_message(
        adapter, _new_message_update(text="please help", msg_id=2))
    assert len(turn_calls) == 1
    # A reply to a business-bot-sent message replies even without the word:
    # the intake computes reply_to_business_bot from update.reply_to_message.
    reply = _tl_message(msg_id=9, text="previous bot reply",
                        via_business_bot_id=BOT_ID)
    update = tl_types.UpdateBotNewBusinessMessage(
        connection_id=CONN_ID,
        message=_tl_message(text="hello again", msg_id=3),
        qts=3,
        reply_to_message=reply,
    )
    await business_adapter.handle_business_new_message(adapter, update)
    assert len(turn_calls) == 2


# ---------------------------------------------------------------------------
# reply_via_turn: final text IS the reply, owner profile pinned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_via_turn_returns_final_text(tmp_path):
    adapter = _adapter(tmp_path)
    turn_calls = _fake_turn(adapter, "  Reply text  ")
    text = await business_adapter.reply_via_turn(
        adapter, "hello", str(CUSTOMER_CHAT),
        {"connection_id": CONN_ID, "customer_name": "Cust", "trigger": "direct",
         "persona": "Be nice.", "recent_messages": [],
         "owner_user_id": str(OWNER_ID)},
    )
    assert text == "Reply text"
    assert adapter._client.requests == []
    assert len(turn_calls) == 1
    event_obj = turn_calls[0]
    assert event_obj.source.thread_id == f"biz-{CONN_ID}-{CUSTOMER_CHAT}"
    assert event_obj.source.chat_type == "dm"
    assert event_obj.source.user_id == str(OWNER_ID)
    assert event_obj.source.profile == "default"
    payload = json.loads(event_obj.text[event_obj.text.index("{"):])
    assert payload["customer_text"] == "hello"
    assert payload["customer_name"] == "Cust"
    assert payload["connection_id"] == CONN_ID
    assert payload["trigger"] == "direct"
    assert payload["persona"] == "Be nice."
    assert "DRAFT" not in event_obj.text
    assert "IS the reply" in event_obj.text


@pytest.mark.asyncio
async def test_reply_via_turn_pins_runner_primary_profile(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.gateway_runner = SimpleNamespace(_primary_profile_name="launch-profile")
    turn_calls = _fake_turn(adapter, "ok")
    await business_adapter.reply_via_turn(
        adapter, "hello", str(CUSTOMER_CHAT),
        {"connection_id": CONN_ID, "owner_user_id": str(OWNER_ID)})
    assert turn_calls[0].source.profile == "launch-profile"
    assert turn_calls[0].source.user_id == str(OWNER_ID)


@pytest.mark.asyncio
async def test_reply_via_turn_keeps_adapter_owner_profile(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._owner_profile = "named-profile"
    turn_calls = _fake_turn(adapter, "ok")
    await business_adapter.reply_via_turn(
        adapter, "hello", str(CUSTOMER_CHAT),
        {"connection_id": CONN_ID, "owner_user_id": str(OWNER_ID)})
    assert turn_calls[0].source.profile == "named-profile"


@pytest.mark.asyncio
async def test_reply_via_turn_owner_prompt_variant(tmp_path):
    adapter = _adapter(tmp_path)
    turn_calls = _fake_turn(adapter, "4")
    await business_adapter.reply_via_turn(
        adapter, "Say what's 2+2?", str(CUSTOMER_CHAT),
        {"connection_id": CONN_ID, "owner_user_id": str(OWNER_ID),
         "trigger": "owner", "owner_text": "Say what's 2+2?"},
    )
    payload = json.loads(turn_calls[0].text[turn_calls[0].text.index("{"):])
    assert payload["owner_text"] == "Say what's 2+2?"
    assert "owner request" in turn_calls[0].text


@pytest.mark.asyncio
async def test_reply_via_turn_raises_when_no_handler(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._message_handler = None
    with pytest.raises(RuntimeError):
        await business_adapter.reply_via_turn(
            adapter, "hello", str(CUSTOMER_CHAT), {"connection_id": CONN_ID})


@pytest.mark.asyncio
async def test_reply_via_turn_timeout_raises(tmp_path):
    adapter = _adapter(tmp_path)

    async def _slow(event_obj):
        await asyncio.Event().wait()

    adapter._message_handler = _slow
    with pytest.raises(TimeoutError):
        await business_adapter.reply_via_turn(
            adapter, "hello", str(CUSTOMER_CHAT), {"connection_id": CONN_ID},
            timeout=0.05)


# ---------------------------------------------------------------------------
# send_business_message: TL shape + error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_business_message_wraps_invoke_request(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._remember_peer(str(CUSTOMER_CHAT), InputPeerUser(CUSTOMER_CHAT, 0))
    await business_adapter.send_business_message(
        adapter, connection_id=CONN_ID, customer_chat_id=str(CUSTOMER_CHAT),
        text="hello customer")
    assert len(adapter._client.requests) == 1
    req = adapter._client.requests[0]
    assert isinstance(req, InvokeWithBusinessConnectionRequest)
    assert req.connection_id == CONN_ID
    inner = req.query
    assert isinstance(inner, SendMessageRequest)
    assert inner.message == "hello customer"
    assert isinstance(inner.peer, InputPeerUser)
    assert inner.peer.user_id == CUSTOMER_CHAT
    assert isinstance(inner.random_id, int)
    raw = bytes(req)
    assert CONN_ID.encode() in raw
    assert b"hello customer" in raw


@pytest.mark.asyncio
async def test_send_business_message_invalid_connection_marks_disabled(tmp_path):
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    adapter._remember_peer(str(CUSTOMER_CHAT), InputPeerUser(CUSTOMER_CHAT, 0))
    adapter._client.fail_with = RuntimeError(
        "RPCError 400: BUSINESS_CONNECTION_INVALID")
    with pytest.raises(RuntimeError, match="no longer valid"):
        await business_adapter.send_business_message(
            adapter, connection_id=CONN_ID, customer_chat_id=str(CUSTOMER_CHAT),
            text="hi")
    row = manager._db.get_telegram_business_connection(CONN_ID)
    assert row["is_enabled"] is False


@pytest.mark.asyncio
async def test_send_business_message_peer_errors_raise_clear_cause(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    adapter._remember_peer(str(CUSTOMER_CHAT), InputPeerUser(CUSTOMER_CHAT, 0))
    adapter._client.fail_with = RuntimeError(
        "RPCError 400: BUSINESS_PEER_USAGE_MISSING")
    with pytest.raises(RuntimeError, match="has not written recently"):
        await business_adapter.send_business_message(
            adapter, connection_id=CONN_ID, customer_chat_id=str(CUSTOMER_CHAT),
            text="hi")


# ---------------------------------------------------------------------------
# hm:biz: callback gate + dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_biz_card_action_dispatches_to_business_ui(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    calls = []

    async def _handle_card_action(ad, event, source, action):
        calls.append(action)

    fake_ui = SimpleNamespace(handle_card_action=_handle_card_action)
    monkeypatch.setitem(
        sys.modules, "plugins.platforms.telegram.business_ui", fake_ui)
    event = _callback_event(data=b"hm:biz:pause")
    await adapter._on_callback_query(event)
    assert calls == ["pause"]


@pytest.mark.asyncio
async def test_biz_callback_mute_applies_and_renders_recent(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    manager._db.record_business_message(
        CONN_ID, str(CUSTOMER_CHAT), "7", "Alt", "hi", "in")
    calls = []

    async def _handle_card_action(ad, event, source, action):
        calls.append(action)

    fake_ui = SimpleNamespace(handle_card_action=_handle_card_action)
    monkeypatch.setitem(
        sys.modules, "plugins.platforms.telegram.business_ui", fake_ui)
    event = _callback_event(data=b"hm:biz:mute:200")
    await adapter._on_callback_query(event)
    assert calls == ["recent"]
    assert manager._db.get_telegram_business_connection(CONN_ID)["paused_chats"] == ["200"]


@pytest.mark.asyncio
async def test_biz_callback_unknown_action_answers(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    calls = []

    async def _handle_card_action(ad, event, source, action):
        calls.append(action)

    fake_ui = SimpleNamespace(handle_card_action=_handle_card_action)
    monkeypatch.setitem(
        sys.modules, "plugins.platforms.telegram.business_ui", fake_ui)
    event = _callback_event(data=b"hm:biz:bogus")
    await adapter._on_callback_query(event)
    assert calls == []
    assert any("Unknown action" in (args[0] if args else "")
               for args, _ in event.answers)


@pytest.mark.asyncio
async def test_biz_callback_non_owner_mute_is_denied(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    fake_ui = SimpleNamespace(handle_card_action=AsyncMock())
    monkeypatch.setitem(
        sys.modules, "plugins.platforms.telegram.business_ui", fake_ui)
    event = _callback_event(data=b"hm:biz:mute:200", sender_id=777)
    await adapter._on_callback_query(event)
    assert any("Unknown customer chat" in (args[0] if args else "")
               for args, _ in event.answers)
    assert manager._db.get_telegram_business_connection(CONN_ID)["paused_chats"] == []
    fake_ui.handle_card_action.assert_not_called()


# ---------------------------------------------------------------------------
# Guest-query routing: business messages never answer as guest DMs (§9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guest_query_in_business_chat_routes_to_business_reply(tmp_path):
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter, "4")
    await business_adapter.handle_business_connect(adapter, _connect_update())
    manager._db.record_business_message(
        CONN_ID, str(CUSTOMER_CHAT), "7", "Alt", "hi", "in")
    adapter._remember_peer(str(CUSTOMER_CHAT), InputPeerUser(CUSTOMER_CHAT, 0))
    adapter._client.requests.clear()
    routed = await business_adapter.maybe_route_guest_query_to_business(
        adapter, chat_id=str(CUSTOMER_CHAT), user_id=str(OWNER_ID),
        peer_type="PeerUser", text="Say what's 2+2?")
    assert routed is True
    assert len(turn_calls) == 1
    assert turn_calls[0].source.thread_id == f"biz-{CONN_ID}-{CUSTOMER_CHAT}"
    assert turn_calls[0].source.user_id == str(OWNER_ID)
    payload = json.loads(turn_calls[0].text[turn_calls[0].text.index("{"):])
    assert payload["trigger"] == "owner"
    delivered = [r for r in adapter._client.requests
                 if isinstance(r, InvokeWithBusinessConnectionRequest)]
    assert len(delivered) == 1
    assert delivered[0].query.message == "4"


@pytest.mark.asyncio
async def test_guest_query_same_chat_not_routed(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    routed = await business_adapter.maybe_route_guest_query_to_business(
        adapter, chat_id=str(CUSTOMER_CHAT), user_id=str(CUSTOMER_CHAT),
        peer_type="PeerUser", text="hey")
    assert routed is False
    assert turn_calls == []


@pytest.mark.asyncio
async def test_guest_query_group_peer_not_routed(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    routed = await business_adapter.maybe_route_guest_query_to_business(
        adapter, chat_id="123", user_id=str(OWNER_ID),
        peer_type="PeerChat", text="hey")
    assert routed is False
    assert turn_calls == []


@pytest.mark.asyncio
async def test_guest_query_no_business_connection_not_routed(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter)
    routed = await business_adapter.maybe_route_guest_query_to_business(
        adapter, chat_id=str(CUSTOMER_CHAT), user_id=str(OWNER_ID),
        peer_type="PeerUser", text="hey")
    assert routed is False
    assert turn_calls == []


@pytest.mark.asyncio
async def test_guest_query_non_owner_invoker_not_routed(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    routed = await business_adapter.maybe_route_guest_query_to_business(
        adapter, chat_id=str(CUSTOMER_CHAT), user_id="777",
        peer_type="PeerUser", text="hey")
    assert routed is False
    assert turn_calls == []


@pytest.mark.asyncio
async def test_guest_handler_routes_business_query_without_guest_answer(tmp_path):
    """End to end through _on_guest_chat_query: no guest status, no guest result."""
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    turn_calls = _fake_turn(adapter, "4")
    await business_adapter.handle_business_connect(adapter, _connect_update())
    manager._db.record_business_message(
        CONN_ID, str(CUSTOMER_CHAT), "7", "Alt", "hi", "in")
    adapter._remember_peer(str(CUSTOMER_CHAT), InputPeerUser(CUSTOMER_CHAT, 0))
    adapter._client.requests.clear()
    await adapter._on_guest_chat_query(_guest_query_update())
    assert len(turn_calls) == 1
    delivered = [r for r in adapter._client.requests
                 if isinstance(r, InvokeWithBusinessConnectionRequest)]
    assert len(delivered) == 1
    assert delivered[0].query.message == "4"
    # The only plain send is the owner-DM sent notice — nothing went to
    # the guest chat, so no guest status message and no guest result.
    sends = [r for r in adapter._client.requests if isinstance(r, SendMessageRequest)]
    assert len(sends) == 1
    from telethon.utils import get_peer_id

    assert get_peer_id(sends[0].peer) == OWNER_ID


# ---------------------------------------------------------------------------
# DM owner-input intercept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_dm_text_consumed_when_owner_input_pending(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    handle_message = AsyncMock()
    adapter.handle_message = handle_message
    adapter._is_owner_user = lambda user_id, source=None: str(user_id) == str(OWNER_ID)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    await manager.handle_callback(
        data="hm:biz:trigger_word", caller_user_id=str(OWNER_ID),
        answer=AsyncMock())
    saved = {}

    def _save(home, changes):
        saved.update(changes)
        return dict(changes)

    monkeypatch.setattr(
        "plugins.platforms.telegram.business_config.save_business_settings", _save)
    event = _dm_event(adapter, text="help")
    await adapter._process_inbound_event(event, [event.message])
    handle_message.assert_not_called()
    assert manager.get_settings()["trigger_word"] == "help"
    assert saved == {"trigger_word": "help"}


@pytest.mark.asyncio
async def test_owner_input_slash_command_passes_through(tmp_path):
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    await manager.handle_callback(
        data="hm:biz:trigger_word", caller_user_id=str(OWNER_ID),
        answer=AsyncMock())
    source = adapter.build_source(str(OWNER_ID), chat_type="dm", user_id=str(OWNER_ID))
    assert await business_adapter.maybe_consume_business_input(
        adapter, source, "/biz") is False
    # The capture stayed armed for the real value.
    assert manager._pending_owner_input[str(OWNER_ID)] == "trigger_word"


@pytest.mark.asyncio
async def test_owner_input_passes_through_when_nothing_pending(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_manager(adapter, tmp_path)
    adapter._is_owner_user = lambda user_id, source=None: str(user_id) == str(OWNER_ID)
    source = adapter.build_source(str(OWNER_ID), chat_type="dm", user_id=str(OWNER_ID))
    assert await business_adapter.maybe_consume_business_input(
        adapter, source, "hello") is False


@pytest.mark.asyncio
async def test_owner_input_ignores_non_owner_dm(tmp_path):
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    manager._pending_owner_input[str(OWNER_ID)] = "trigger_word"
    adapter._is_owner_user = lambda user_id, source=None: False
    # Synthetic source: the owner DM chat but a different sender — the
    # owner gate must refuse it even though a capture is pending.
    source = adapter.build_source(str(OWNER_ID), chat_type="dm", user_id="777")
    assert await business_adapter.maybe_consume_business_input(
        adapter, source, "hello") is False


# ---------------------------------------------------------------------------
# /biz routing
# ---------------------------------------------------------------------------


def test_biz_is_a_native_operation_command():
    assert "biz" in TELEGRAM_OPERATION_COMMANDS


@pytest.mark.asyncio
async def test_biz_routes_to_business_ui(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    calls = []

    async def _handle_command(ad, message, source, command, arguments, reply_to=None):
        calls.append((ad is adapter, command, arguments, reply_to))
        return True

    fake_ui = SimpleNamespace(handle_command=_handle_command)
    monkeypatch.setitem(
        sys.modules, "plugins.platforms.telegram.business_ui", fake_ui)
    adapter._authorized = lambda source: True
    source = SimpleNamespace(chat_id=str(OWNER_ID), chat_type="dm",
                             user_id=str(OWNER_ID), is_bot=False,
                             profile_route_rejected=False)
    message = SimpleNamespace(message="/biz status", id=9)
    assert await adapter._handle_telegram_operation_command(
        message, source, "biz", "status", reply_to=9) is True
    assert calls == [(True, "biz", "status", 9)]


@pytest.mark.asyncio
async def test_biz_refused_for_unadmitted_sender(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    fake_ui = SimpleNamespace(handle_command=AsyncMock(return_value=True))
    monkeypatch.setitem(
        sys.modules, "plugins.platforms.telegram.business_ui", fake_ui)
    adapter._authorized = lambda source: False
    source = SimpleNamespace(chat_id="777", chat_type="dm", user_id="777",
                             is_bot=False, profile_route_rejected=False)
    message = SimpleNamespace(message="/biz", id=9)
    assert await adapter._handle_telegram_operation_command(
        message, source, "biz", "", reply_to=9) is True
    fake_ui.handle_command.assert_not_called()
