"""Telethon-native intake tests for Telegram Business updates.

Business updates (``UpdateBotNewBusinessMessage`` and friends) arrive on
the bot's own account: they are the owner's chats with third parties,
never a DM to the bot. The guard is absolute — a business message never
becomes an owner DM, never starts an owner turn, and never reaches the
normal text pipeline. These tests drive the live intake handlers in
``business_adapter`` against the real manager (debounce 0) with a fake
turn and a fake client that records every TL request.
"""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types as tl_types
from telethon.tl.functions import InvokeWithBusinessConnectionRequest
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.types import (
    BotBusinessConnection,
    BusinessBotRights,
    Message,
    PeerChat,
    PeerUser,
)

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


@pytest.fixture()
def adapter(tmp_path):
    ad = TelethonAdapter(PlatformConfig(enabled=True, token="x"))
    ad._client = _FakeClient()
    ad._telegram_transport_home = tmp_path
    ad._bot_id = BOT_ID
    db = BusinessStateDB(tmp_path / "biz_state.db")
    manager = BusinessModeManager(
        db,
        send_message=partial(business_adapter.send_owner_message, ad),
        reply_via_turn=partial(business_adapter.reply_via_turn, ad),
        send_business_message=partial(business_adapter.send_business_message, ad),
        debounce_seconds=0.0,
    )
    ad._telegram_business_manager = manager
    ad._telegram_business_manager_home = ad._telegram_transport_home
    turn_calls = []

    async def _turn(event_obj):
        turn_calls.append(event_obj)
        return SimpleNamespace(text="Reply text")

    ad._message_handler = _turn
    ad._turn_calls = turn_calls
    ad._manager = manager
    return ad


def _connect(*, reply=True):
    return tl_types.UpdateBotBusinessConnect(
        connection=BotBusinessConnection(
            connection_id=CONN_ID, user_id=OWNER_ID, dc_id=1, date=None,
            disabled=False, rights=BusinessBotRights(reply=reply)),
        qts=1,
    )


def _tl_message(*, msg_id=1, text="Hi there", from_id=None,
                via_business_bot_id=None, peer=None):
    return Message(
        id=msg_id,
        peer_id=peer if peer is not None else PeerUser(CUSTOMER_CHAT),
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
        from_id=from_id if from_id is not None else PeerUser(CUSTOMER_ID),
        via_business_bot_id=via_business_bot_id,
    )


def _new_message(*, text="Hi there", msg_id=1, **kwargs):
    return tl_types.UpdateBotNewBusinessMessage(
        connection_id=CONN_ID, message=_tl_message(text=text, msg_id=msg_id, **kwargs),
        qts=2)


def _owner_dm_sends(adapter):
    return [r for r in adapter._client.requests if isinstance(r, SendMessageRequest)]


def _customer_deliveries(adapter):
    return [r for r in adapter._client.requests
            if isinstance(r, InvokeWithBusinessConnectionRequest)]


@pytest.mark.asyncio
async def test_customer_message_direct_replies_not_owner_turn(adapter):
    await business_adapter.handle_business_connect(adapter, _connect())
    adapter._client.requests.clear()
    await business_adapter.handle_business_new_message(
        adapter, _new_message(text="Do you deliver on Sunday?"))
    assert len(adapter._turn_calls) == 1
    event_obj = adapter._turn_calls[0]
    assert event_obj.source.thread_id == f"biz-{CONN_ID}-{CUSTOMER_CHAT}"
    # The turn runs as the OWNER, never as the customer.
    assert event_obj.source.user_id == str(OWNER_ID)
    # The reply went directly to the customer via the business connection.
    delivered = _customer_deliveries(adapter)
    assert len(delivered) == 1
    assert delivered[0].query.message == "Reply text"
    # The owner DM got only the minimal sent notice.
    sends = _owner_dm_sends(adapter)
    assert len(sends) == 1
    assert "Sent to" in sends[0].message


@pytest.mark.asyncio
async def test_owner_written_business_message_never_starts_a_turn(adapter):
    await business_adapter.handle_business_connect(adapter, _connect())
    adapter._client.requests.clear()
    await business_adapter.handle_business_new_message(
        adapter, _new_message(text="dinner tonight?", from_id=PeerUser(OWNER_ID)))
    assert adapter._turn_calls == []
    assert _owner_dm_sends(adapter) == []


@pytest.mark.asyncio
async def test_bot_sent_business_message_never_starts_a_turn(adapter):
    await business_adapter.handle_business_connect(adapter, _connect())
    adapter._client.requests.clear()
    await business_adapter.handle_business_new_message(
        adapter, _new_message(text="approved reply", via_business_bot_id=BOT_ID))
    assert adapter._turn_calls == []
    assert _owner_dm_sends(adapter) == []


@pytest.mark.asyncio
async def test_edited_customer_message_re_replies(adapter):
    await business_adapter.handle_business_connect(adapter, _connect())
    update = tl_types.UpdateBotEditBusinessMessage(
        connection_id=CONN_ID,
        message=_tl_message(text="Do you deliver on Monday?", msg_id=3),
        qts=4,
    )
    await business_adapter.handle_business_new_message(adapter, update)
    assert len(adapter._turn_calls) == 1
    assert _customer_deliveries(adapter)


@pytest.mark.asyncio
async def test_group_business_message_is_logged_and_ignored(adapter):
    await business_adapter.handle_business_connect(adapter, _connect())
    adapter._client.requests.clear()
    update = tl_types.UpdateBotNewBusinessMessage(
        connection_id=CONN_ID,
        message=_tl_message(text="group hello", peer=PeerChat(555)),
        qts=2,
    )
    await business_adapter.handle_business_new_message(adapter, update)
    assert adapter._turn_calls == []
    assert _owner_dm_sends(adapter) == []


@pytest.mark.asyncio
async def test_delete_is_consumed_with_no_draft_to_discard(adapter):
    await business_adapter.handle_business_connect(adapter, _connect())
    await business_adapter.handle_business_new_message(
        adapter, _new_message(text="please hold this", msg_id=7))
    assert len(adapter._manager.recent_messages(str(OWNER_ID))) == 2
    delete = tl_types.UpdateBotDeleteBusinessMessage(
        connection_id=CONN_ID, peer=PeerUser(CUSTOMER_CHAT), messages=[7], qts=5)
    discarded = await business_adapter.handle_business_delete(adapter, delete)
    assert discarded == 0
    assert len(adapter._manager.recent_messages(str(OWNER_ID))) == 2


@pytest.mark.asyncio
async def test_business_callback_query_starts_no_turn_and_no_reply(adapter):
    await business_adapter.handle_business_connect(adapter, _connect())
    adapter._client.requests.clear()
    update = tl_types.UpdateBusinessBotCallbackQuery(
        query_id=1, user_id=CUSTOMER_ID, connection_id=CONN_ID,
        message=_tl_message(text="button host"), chat_instance=12345, data=b"tap")
    await business_adapter.handle_business_callback_query(adapter, update)
    assert adapter._turn_calls == []
    assert adapter._client.requests == []


@pytest.mark.asyncio
async def test_business_update_never_reaches_the_normal_text_pipeline(adapter):
    """The Raw handlers claim business updates: no owner-DM turn is spawned.

    The adapter's normal inbound entry point is never invoked for a
    business update — the intake handler is the only consumer.
    """
    await business_adapter.handle_business_connect(adapter, _connect())
    handle_message = AsyncMock()
    adapter.handle_message = handle_message
    await business_adapter.handle_business_new_message(
        adapter, _new_message(text="hello"))
    await business_adapter.handle_business_new_message(
        adapter, _new_message(text="owner text", from_id=PeerUser(OWNER_ID)))
    handle_message.assert_not_called()
