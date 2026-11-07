"""Behaviour contracts for the Telegram widget tap loop (layer 233).

Covers the normaliser, the always-answer dispatch, per-widget
serialisation, the shared-edit throttle, and the last-known-state cache in
``widget_queries.py`` plus the adapter's thin Raw delegates: every tap gets
an answer even when nothing can serve it, and a tap on a forward carries
only (query_id, query_type, user_id, data, widget_id) — never a message.
"""
import asyncio

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _FakeClock:
    """Injectable monotonic clock: tests control time, so none of them sleep."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _WidgetFakeClient:
    """Fake Telethon client: records requests, answers entity lookups."""

    def __init__(self):
        self.sent = []
        self.handlers = []

    def add_event_handler(self, handler, event):
        self.handlers.append((handler, event))

    async def get_input_entity(self, peer):
        from telethon import types

        if isinstance(peer, int) and peer < 0:
            return types.InputPeerChannel(channel_id=abs(peer), access_hash=123)
        return types.InputPeerUser(user_id=int(peer), access_hash=123)

    async def get_entity(self, peer):
        return None

    async def __call__(self, request):
        self.sent.append(request)


def _query_type_data():
    from telethon import types

    return types.WidgetQueryTypeData()


def _original_query(*, query_id=11, widget_id=987654321, data=b"vote:1"):
    from telethon import types

    return types.UpdateBotWidgetQuery(
        query_id=query_id, query_type=_query_type_data(), user_id=42,
        peer=types.PeerUser(user_id=42), msg_id=77, chat_instance=99,
        chat_type="private", data=data, widget_id=widget_id,
    )


def _inline_query(*, query_id=12, widget_id=987654321):
    from telethon import types

    return types.UpdateInlineBotWidgetQuery(
        query_id=query_id, query_type=_query_type_data(), user_id=42,
        inline_message_id=types.InputBotInlineMessageID(dc_id=2, id=777, access_hash=999),
        chat_instance=99, chat_type="private", data=b"vote:0", widget_id=widget_id,
    )


def _copy_query(*, query_id=13, widget_id=987654321):
    from telethon import types

    return types.UpdateBotWidgetCopyQuery(
        query_id=query_id, query_type=_query_type_data(), user_id=42,
        data=b"vote:1", widget_id=widget_id,
    )


def _adapter_with_fake_client():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = _WidgetFakeClient()
    return adapter


# --- Normalisation -------------------------------------------------------


def test_normalise_original_query_keeps_coordinates():
    from plugins.platforms.telegram.widget_queries import normalise_widget_query

    query = normalise_widget_query(_original_query())

    assert query is not None
    assert query.kind == "original"
    assert query.query_id == 11
    assert query.query_type == "data"
    assert query.user_id == 42
    assert query.data == b"vote:1"
    assert query.widget_id == 987654321
    assert query.msg_id == 77
    assert query.chat_type == "private"
    assert type(query.peer).__name__ == "PeerUser"
    assert query.inline_message_id is None


def test_normalise_inline_query_keeps_inline_id_not_peer():
    from plugins.platforms.telegram.widget_queries import normalise_widget_query

    query = normalise_widget_query(_inline_query())

    assert query is not None
    assert query.kind == "inline"
    assert query.query_id == 12
    assert query.inline_message_id is not None
    assert query.inline_message_id.id == 777
    assert query.peer is None
    assert query.msg_id is None


def test_normalise_copy_query_carries_only_widget_identity():
    from plugins.platforms.telegram.widget_queries import normalise_widget_query

    update = _copy_query()
    # The TL type has no msg_id / peer attributes at all: touching them
    # would raise AttributeError, so a successful normalisation proves the
    # copy path never reads them.
    assert not hasattr(update, "msg_id")
    assert not hasattr(update, "peer")

    query = normalise_widget_query(update)

    assert query is not None
    assert query.kind == "copy"
    assert query.query_id == 13
    assert query.user_id == 42
    assert query.data == b"vote:1"
    assert query.widget_id == 987654321
    assert query.peer is None
    assert query.msg_id is None
    assert query.inline_message_id is None
    assert query.chat_type is None


def test_normalise_unknown_update_returns_none():
    from plugins.platforms.telegram.widget_queries import normalise_widget_query
    from telethon import types

    update = types.UpdateChannelTooLong(channel_id=1)
    assert normalise_widget_query(update) is None
    assert normalise_widget_query(object()) is None


# --- Dispatch: always answer ---------------------------------------------


@pytest.mark.asyncio
async def test_handler_result_is_the_answer():
    from plugins.platforms.telegram.widget_queries import (
        handle_widget_query, register_widget_query_handler,
    )

    adapter = _adapter_with_fake_client()
    seen = []

    async def handler(query):
        seen.append(query)
        return '{"my": 1, "r": 2}'

    register_widget_query_handler(adapter, handler)
    await handle_widget_query(adapter, _original_query())

    assert [query.kind for query in seen] == ["original"]
    assert [type(r).__name__ for r in adapter._client.sent] == ["SetBotWidgetQueryAnswerRequest"]
    assert adapter._client.sent[0].query_id == 11
    assert adapter._client.sent[0].result == '{"my": 1, "r": 2}'


@pytest.mark.asyncio
async def test_unknown_widget_with_no_handler_gets_empty_answer():
    from plugins.platforms.telegram.widget_queries import handle_widget_query

    adapter = _adapter_with_fake_client()
    # widget_id 987654321 is known to no handler and no registry: the tap
    # is still answered, with the empty answer.
    await handle_widget_query(adapter, _copy_query())

    assert len(adapter._client.sent) == 1
    assert type(adapter._client.sent[0]).__name__ == "SetBotWidgetQueryAnswerRequest"
    assert adapter._client.sent[0].query_id == 13
    assert adapter._client.sent[0].result is None


@pytest.mark.asyncio
async def test_raising_handler_still_gets_empty_answer():
    from plugins.platforms.telegram.widget_queries import (
        handle_widget_query, register_widget_query_handler,
    )

    adapter = _adapter_with_fake_client()

    async def handler(query):
        raise RuntimeError("poll state is gone")

    register_widget_query_handler(adapter, handler)
    await handle_widget_query(adapter, _original_query())  # must not raise

    assert len(adapter._client.sent) == 1
    assert adapter._client.sent[0].result is None


@pytest.mark.asyncio
async def test_oversize_handler_result_falls_back_to_empty_answer():
    from plugins.platforms.telegram.widget_queries import (
        handle_widget_query, register_widget_query_handler,
    )

    adapter = _adapter_with_fake_client()

    async def handler(query):
        return "x" * 1_025  # over the 1,024-byte answer limit

    register_widget_query_handler(adapter, handler)
    await handle_widget_query(adapter, _original_query())

    assert len(adapter._client.sent) == 1
    assert adapter._client.sent[0].result is None


@pytest.mark.asyncio
async def test_register_and_unregister_handler():
    from plugins.platforms.telegram.widget_queries import (
        handle_widget_query, register_widget_query_handler,
        unregister_widget_query_handler,
    )

    adapter = _adapter_with_fake_client()

    async def handler(query):
        return '{"ok": true}'

    register_widget_query_handler(adapter, handler)
    await handle_widget_query(adapter, _original_query())
    assert adapter._client.sent[0].result == '{"ok": true}'

    unregister_widget_query_handler(adapter)
    await handle_widget_query(adapter, _original_query(query_id=14))
    assert adapter._client.sent[1].result is None


# --- Per-widget serialisation + shared-edit throttle ----------------------


@pytest.mark.asyncio
async def test_concurrent_taps_on_same_widget_serialise():
    from plugins.platforms.telegram.widget_queries import (
        handle_widget_query, register_widget_query_handler,
    )

    adapter = _adapter_with_fake_client()
    order = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(query):
        order.append(("enter", query.query_id))
        if query.query_id == 11:
            first_entered.set()
            await release_first.wait()
        order.append(("exit", query.query_id))

    register_widget_query_handler(adapter, handler)
    first = asyncio.create_task(handle_widget_query(adapter, _original_query(query_id=11)))
    await first_entered.wait()
    second = asyncio.create_task(handle_widget_query(adapter, _original_query(query_id=12)))
    for _ in range(10):
        await asyncio.sleep(0)
    # The second tap is queued behind the first tap's lock: it has not
    # entered the handler while the first is still running.
    assert order == [("enter", 11)]
    release_first.set()
    await asyncio.gather(first, second)
    assert order == [("enter", 11), ("exit", 11), ("enter", 12), ("exit", 12)]
    assert len(adapter._client.sent) == 2


@pytest.mark.asyncio
async def test_shared_edit_throttle_delay_math():
    from plugins.platforms.telegram.widget_queries import WidgetQueryLoop

    clock = _FakeClock(now=1000.0)
    adapter = _adapter_with_fake_client()
    loop = WidgetQueryLoop(adapter, time_fn=clock)

    result = await loop.edit_shared_data(987654321, '{"r": 2}')
    assert result.success
    assert type(adapter._client.sent[0]).__name__ == "EditWidgetDataRequest"

    clock.now = 1000.5
    assert loop.shared_edit_delay_s(987654321) == pytest.approx(0.5)
    clock.now = 1001.0
    assert loop.shared_edit_delay_s(987654321) == 0.0
    clock.now = 1002.0
    result = await loop.edit_shared_data(987654321, '{"r": 3}')
    assert result.success
    assert len(adapter._client.sent) == 2


def test_per_widget_lock_table_is_bounded():
    from plugins.platforms.telegram.widget_queries import WidgetQueryLoop

    adapter = _adapter_with_fake_client()
    loop = WidgetQueryLoop(adapter, max_locks=2)
    loop._lock_for(1)
    loop._lock_for(2)
    loop._lock_for(3)
    assert len(loop._locks) == 2
    assert 1 not in loop._locks


# --- Last-known-state cache ------------------------------------------------


def _data_update(*, msg_id=77, public_data='{"r": 2}', chat_data=None, pts=5):
    from telethon import types

    kwargs = {"peer": types.PeerUser(user_id=42), "msg_id": msg_id, "pts": pts, "pts_count": 1}
    if public_data is not None:
        kwargs["public_data"] = public_data
        kwargs["public_data_hash"] = 4242
    if chat_data is not None:
        kwargs["chat_data"] = chat_data
    return types.UpdateMessageWidgetData(**kwargs)


@pytest.mark.asyncio
async def test_data_update_is_cached_for_later_read():
    from plugins.platforms.telegram.widget_queries import (
        get_widget_state, handle_widget_data_update,
    )

    adapter = _adapter_with_fake_client()
    await handle_widget_data_update(adapter, _data_update(chat_data='{"my": 1}'))

    state = get_widget_state(adapter, 42, 77)
    assert state is not None
    assert state.public_data == '{"r": 2}'
    assert state.public_data_hash == 4242
    assert state.chat_data == '{"my": 1}'
    assert state.pts == 5
    assert get_widget_state(adapter, 42, 78) is None


@pytest.mark.asyncio
async def test_partial_data_update_merges_instead_of_clobbering():
    from plugins.platforms.telegram.widget_queries import (
        get_widget_state, handle_widget_data_update,
    )

    adapter = _adapter_with_fake_client()
    await handle_widget_data_update(adapter, _data_update())
    # A chat_data-only update carries no public_data: the cached shared
    # state must survive it.
    await handle_widget_data_update(
        adapter, _data_update(public_data=None, chat_data='{"my": 0}', pts=6))

    state = get_widget_state(adapter, 42, 77)
    assert state is not None
    assert state.public_data == '{"r": 2}'
    assert state.chat_data == '{"my": 0}'
    assert state.pts == 6


def test_state_cache_evicts_oldest_entries():
    from plugins.platforms.telegram.widget_queries import WidgetQueryLoop

    adapter = _adapter_with_fake_client()
    loop = WidgetQueryLoop(adapter, max_states=2)
    loop.record_widget_data_update(_data_update(msg_id=1))
    loop.record_widget_data_update(_data_update(msg_id=2))
    loop.record_widget_data_update(_data_update(msg_id=3))
    assert loop.get_widget_state(42, 1) is None
    assert loop.get_widget_state(42, 2) is not None
    assert loop.get_widget_state(42, 3) is not None


# --- Wire construction -----------------------------------------------------


def test_routed_updates_and_answer_serialise():
    from telethon.extensions.binaryreader import BinaryReader

    from plugins.platforms.telegram.widget_messages import answer_widget_query_request

    for update in (_original_query(), _inline_query(), _copy_query(), _data_update()):
        data = bytes(update)
        back = BinaryReader(data).tgread_object()
        assert bytes(back) == data
    request = answer_widget_query_request(11, '{"ok": true}')
    data = bytes(request)
    assert BinaryReader(data).tgread_object().result == '{"ok": true}'


# --- Adapter wiring --------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_delegate_answers_through_the_loop():
    adapter = _adapter_with_fake_client()
    await adapter._on_widget_query(_original_query())
    assert len(adapter._client.sent) == 1
    assert adapter._client.sent[0].result is None


@pytest.mark.asyncio
async def test_adapter_delegate_records_data_update():
    from plugins.platforms.telegram.widget_queries import get_widget_state

    adapter = _adapter_with_fake_client()
    await adapter._on_widget_data_update(_data_update())
    assert get_widget_state(adapter, 42, 77) is not None


def test_adapter_registers_widget_raw_handlers():
    from telethon import events
    from telethon import types as tl_types

    adapter = _adapter_with_fake_client()
    adapter._register_handlers(events)

    raw_types = set()
    for handler, event in adapter._client.handlers:
        if type(event).__name__ == "Raw":
            raw_types.add(event.types)
    assert raw_types >= {
        tl_types.UpdateBotWidgetQuery,
        tl_types.UpdateInlineBotWidgetQuery,
        tl_types.UpdateBotWidgetCopyQuery,
        tl_types.UpdateMessageWidgetData,
    }
    widget_handlers = {
        handler for handler, event in adapter._client.handlers
        if type(event).__name__ == "Raw" and event.types in raw_types
    }
    assert adapter._on_widget_query in widget_handlers
    assert adapter._on_widget_data_update in widget_handlers
