"""Integration seams of the business-bots framework.

Wave 1 (state/manager), wave 2A (adapter), wave 2B (UI) and wave 2C
(tool/config) were built on the same base in parallel, so the seams
between them are pinned here against the REAL modules — no stubbed
``business_ui`` in ``sys.modules``:

- ``/biz`` routing (operations_routing) reaches the real UI and renders
  a status card.
- ``hm:biz:open:<id>`` is unknown in v2 (the draft route is gone) instead
  of reopening a draft.
- ``hm:biz:status`` (a card action) renders through the real UI instead
  of answering "Business controls are unavailable right now."

No network: the client is a fake that records every TL request.
"""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.functions import InvokeWithBusinessConnectionRequest
from telethon.tl.types import (
    BotBusinessConnection,
    BusinessBotRights,
    InputPeerUser,
    Message,
    PeerUser,
)
from telethon.tl import types as tl_types

from gateway.config import PlatformConfig
from plugins.platforms.telegram import business_adapter
from plugins.platforms.telegram.business_manager import BusinessModeManager
from plugins.platforms.telegram.business_state import BusinessStateDB
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

OWNER_ID = 42
CUSTOMER_ID = 200
CUSTOMER_CHAT = 200
CONN_ID = "conn-1"
BOT_ID = 999999


class _FakeClient:
    """Records every TL request; never touches the network."""

    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=1001)


def _adapter(tmp_path):
    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="x"))
    adapter._client = _FakeClient()
    adapter._telegram_transport_home = tmp_path
    adapter._bot_id = BOT_ID
    adapter._authorized = lambda source: True
    adapter._is_owner_user = lambda user_id, source=None: str(user_id) == str(OWNER_ID)
    return adapter


def _seed_manager(adapter, tmp_path):
    """A real manager wired to the adapter's injected callables."""
    db = BusinessStateDB(tmp_path / "biz_state.db")
    manager = BusinessModeManager(
        db,
        send_message=partial(business_adapter.send_owner_message, adapter),
        reply_via_turn=partial(business_adapter.reply_via_turn, adapter),
        send_business_message=partial(business_adapter.send_business_message, adapter),
        debounce_seconds=0.0,
        trigger_mode="direct",
        trigger_word="",
    )
    adapter._telegram_business_manager = manager
    adapter._telegram_business_manager_home = adapter._telegram_transport_home
    return manager


def _tl_connection(*, conn_id=CONN_ID, owner_id=OWNER_ID, enabled=True, reply=True):
    return BotBusinessConnection(
        connection_id=conn_id,
        user_id=owner_id,
        dc_id=1,
        date=None,
        disabled=not enabled,
        rights=BusinessBotRights(reply=reply),
    )


def _connect_update(**kwargs):
    return tl_types.UpdateBotBusinessConnect(connection=_tl_connection(**kwargs), qts=1)


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


def _seed_conversation(manager):
    manager._db.record_business_message(
        CONN_ID, str(CUSTOMER_CHAT), "1", "Cust", "Hi there", "in")
    manager._db.record_business_message(
        CONN_ID, str(CUSTOMER_CHAT), None, "Cust", "Hello back", "out")


def _answers(event):
    return [args[0] if args else "" for args, _ in event.answers]


@pytest.mark.asyncio
async def test_biz_command_reaches_real_ui_and_renders_status(tmp_path):
    """``/biz`` routes to the real UI module function and sends a card."""
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    _seed_conversation(manager)
    source = SimpleNamespace(chat_id=str(OWNER_ID), chat_type="dm",
                             user_id=str(OWNER_ID), is_bot=False,
                             profile_route_rejected=False)
    message = SimpleNamespace(message="/biz", id=9)
    assert await adapter._handle_telegram_operation_command(
        message, source, "biz", "", reply_to=9) is True
    sends = [r for r in adapter._client.requests if hasattr(r, "message")]
    assert sends and "Business" in sends[-1].message


@pytest.mark.asyncio
async def test_biz_open_callback_is_unknown_in_v2(tmp_path):
    """``hm:biz:open:<id>`` is gone with the drafts — it answers "Unknown action."."""
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    _seed_conversation(manager)
    adapter._client.requests.clear()
    event = _callback_event(data=b"hm:biz:open:1")
    await adapter._on_callback_query(event)
    assert "Unknown action." in _answers(event)
    assert adapter._client.requests == []


@pytest.mark.asyncio
async def test_biz_card_action_renders_through_real_ui(tmp_path):
    """``hm:biz:status`` renders the status view — no "unavailable" answer."""
    adapter = _adapter(tmp_path)
    manager = _seed_manager(adapter, tmp_path)
    await business_adapter.handle_business_connect(adapter, _connect_update())
    _seed_conversation(manager)
    adapter._client.requests.clear()
    event = _callback_event(data=b"hm:biz:status")
    await adapter._on_callback_query(event)
    assert "Business controls are unavailable right now." not in _answers(event)
    sends = [r for r in adapter._client.requests if hasattr(r, "message")]
    assert sends and "Business" in sends[-1].message
