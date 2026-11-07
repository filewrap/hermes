"""Tap loop for Telegram widgets (layer 233).

Kept out of ``telethon_adapter.py`` (already over the file-size cap): the
adapter only routes raw updates here and owns the client calls. This module
owns the tap → answer loop from the widget guides (§7–§9):

* ``UpdateBotWidgetQuery`` / ``UpdateInlineBotWidgetQuery`` /
  ``UpdateBotWidgetCopyQuery`` are normalised into one ``WidgetQuery`` and
  handed to a single registered handler; the handler's return value is the
  answer. Every tap is answered — with the empty answer when nothing can
  serve it, when the handler raises, or when no handler is registered — or
  the widget times out after 15 s.
* A copy query (a tap on a forward) carries ONLY ``query_id``,
  ``query_type``, ``user_id``, ``data`` and ``widget_id`` — no message, no
  peer. Count by ``(widget_id, user_id)``; never read ``msg_id``/``peer``
  on it.
* EDIT FIRST, THEN ANSWER. An answer only resolves the tapping widget's
  promise — it changes nothing on screen. A handler that wants a tap to
  change what every copy shows edits state itself (``adapter.
  edit_shared_widget_data`` / ``edit_widget_message_data`` /
  ``edit_inline_widget_data``) BEFORE returning the answer, and puts a
  revision in the state so a stale poll cannot jump the counts backwards
  (guide §8.1, §9).
* Concurrent taps on the same shared object apply in order under one
  asyncio.Lock per ``widget_id`` (bounded table), and shared-object edits
  the loop performs are throttled to at most one per second per object
  (guide §8.1). The throttle gates only the loop's own
  ``edit_shared_data`` path, never the adapter's direct edit methods.
* ``UpdateMessageWidgetData`` is recorded in a bounded last-known-state
  cache keyed by ``(peer_id, msg_id)``; the tool surface reads it later
  through ``get_widget_state``. Nothing else consumes it.

Wallet query types (``sent_transaction`` / ``sign_data``) arrive as
``WidgetQueryType*`` values but the server API is not shipped, so they are
never acted on here — a handler that sees one should answer empty. No
wallet code is built in this module.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Query-type names the handler sees: the TL object is reduced to its kind.
# "data" is the plain tap; the other two are wallet (framework-only).
_WIDGET_QUERY_TYPE_NAMES = {
    "WidgetQueryTypeData": "data",
    "WidgetQueryTypeSentTransaction": "sent_transaction",
    "WidgetQueryTypeSignData": "sign_data",
}
# Per-widget lock table and last-known-state cache caps.
MAX_WIDGET_LOCKS = 128
MAX_WIDGET_STATES = 512
# Shared objects accept at most one EditWidgetData per second (guide §8.1).
MIN_SHARED_EDIT_INTERVAL_S = 1.0


@dataclass(frozen=True)
class WidgetQuery:
    """One widget tap, normalised from any of the three query updates.

    ``peer`` / ``msg_id`` are set only for an original tap, ``inline_message_id``
    only for a via-bot tap; a copy tap has neither (see the module docstring).
    ``query_type`` is ``"data"`` for a plain tap, ``"sent_transaction`` /
    ``"sign_data"`` for wallet taps, or the raw TL class name when unknown.
    """

    query_id: int
    kind: str  # "original" | "inline" | "copy"
    query_type: Optional[str]
    user_id: int
    data: bytes
    widget_id: Optional[int]
    peer: Any
    msg_id: Optional[int]
    inline_message_id: Any
    chat_type: Optional[str]


@dataclass(frozen=True)
class WidgetState:
    """Last-known state of one widget message, from UpdateMessageWidgetData."""

    public_data: Optional[str]
    public_data_hash: Optional[int]
    chat_data: Optional[str]
    pts: Optional[int]


WidgetQueryHandler = Callable[[WidgetQuery], Awaitable[Optional[str]]]


def _query_type_name(query_type: Any) -> Optional[str]:
    if query_type is None:
        return None
    return _WIDGET_QUERY_TYPE_NAMES.get(type(query_type).__name__,
                                        type(query_type).__name__)


def _as_bytes(data: Any) -> bytes:
    if data is None:
        return b""
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    return bytes(data)


def _peer_id(peer: Any) -> Any:
    try:
        from telethon.utils import get_peer_id
        return get_peer_id(peer)
    except (AttributeError, TypeError, ValueError):
        # get_peer_id raises TypeError for uncastable peers (its
        # _raise_cast_fail) and attribute errors on exotic shapes; either
        # way the caller only needs a stable cache key.
        return str(peer)


def normalise_widget_query(update: Any) -> Optional[WidgetQuery]:
    """Normalise a widget query update, or None when it is not one.

    A copy update is built only from the five fields it carries; ``msg_id``
    and ``peer`` are never touched on it (the TL type has no such fields).
    """
    from telethon import types as tl_types

    query_id = getattr(update, "query_id", None)
    if query_id is None:
        return None
    query_type = _query_type_name(getattr(update, "query_type", None))
    user_id = getattr(update, "user_id", None)
    data = _as_bytes(getattr(update, "data", b""))
    widget_id = getattr(update, "widget_id", None)
    if isinstance(update, tl_types.UpdateBotWidgetQuery):
        return WidgetQuery(
            query_id=int(query_id), kind="original", query_type=query_type,
            user_id=int(user_id), data=data,
            widget_id=int(widget_id) if widget_id is not None else None,
            peer=getattr(update, "peer", None),
            msg_id=getattr(update, "msg_id", None),
            inline_message_id=None,
            chat_type=getattr(update, "chat_type", None) or None,
        )
    if isinstance(update, tl_types.UpdateInlineBotWidgetQuery):
        return WidgetQuery(
            query_id=int(query_id), kind="inline", query_type=query_type,
            user_id=int(user_id), data=data,
            widget_id=int(widget_id) if widget_id is not None else None,
            peer=None, msg_id=None,
            inline_message_id=getattr(update, "inline_message_id", None),
            chat_type=getattr(update, "chat_type", None) or None,
        )
    if isinstance(update, tl_types.UpdateBotWidgetCopyQuery):
        return WidgetQuery(
            query_id=int(query_id), kind="copy", query_type=query_type,
            user_id=int(user_id), data=data,
            widget_id=int(widget_id) if widget_id is not None else None,
            peer=None, msg_id=None, inline_message_id=None, chat_type=None,
        )
    return None


class WidgetQueryLoop:
    """Per-adapter tap-loop state: handler, per-widget locks, throttle, cache.

    One loop per adapter, held on the adapter as ``_widget_query_loop``
    (created lazily by ``get_widget_query_loop``). ``time_fn`` is injectable
    like ``ResponseStreamDraft``'s, so the throttle needs no sleeps in tests.
    """

    def __init__(self, adapter: Any, *, time_fn=time.monotonic,
                 max_locks: int = MAX_WIDGET_LOCKS,
                 max_states: int = MAX_WIDGET_STATES,
                 min_shared_edit_interval_s: float = MIN_SHARED_EDIT_INTERVAL_S) -> None:
        self._adapter = adapter
        self._time_fn = time_fn
        self._max_locks = max(1, int(max_locks))
        self._max_states = max(1, int(max_states))
        self._min_shared_edit_interval_s = float(min_shared_edit_interval_s)
        self.handler: Optional[WidgetQueryHandler] = None
        self._locks: OrderedDict[int, asyncio.Lock] = OrderedDict()
        self._last_shared_edit_at: dict[int, float] = {}
        self._states: OrderedDict[Any, WidgetState] = OrderedDict()

    def _lock_for(self, widget_id: int) -> asyncio.Lock:
        """Return the serialising lock for one shared object, evicting idle locks."""
        lock = self._locks.get(widget_id)
        if lock is not None:
            self._locks.move_to_end(widget_id)
            return lock
        lock = asyncio.Lock()
        self._locks[widget_id] = lock
        while len(self._locks) > self._max_locks:
            oldest_id, oldest = next(iter(self._locks.items()))
            if oldest.locked():
                # Every lock is in use; let the table grow by this one entry
                # rather than evicting a lock a tap is waiting on.
                break
            self._locks.pop(oldest_id)
        return lock

    def shared_edit_delay_s(self, widget_id: int) -> float:
        """Seconds to wait before the next shared edit of this object (0 = now)."""
        last = self._last_shared_edit_at.get(int(widget_id))
        if last is None:
            return 0.0
        return max(0.0, self._min_shared_edit_interval_s - (self._time_fn() - last))

    async def edit_shared_data(self, widget_id: int, public_data: str, *,
                               cache_time: Optional[int] = None) -> Any:
        """Edit a shared object through the loop's throttle.

        Call from inside a handler: dispatch already holds this object's
        lock, so the throttle wait keeps taps in order instead of racing
        them. Records the edit time only on success — a refused edit does
        not consume the object's one-edit-per-second budget.
        """
        delay = self.shared_edit_delay_s(widget_id)
        if delay > 0:
            await asyncio.sleep(delay)
        result = await self._adapter.edit_shared_widget_data(
            widget_id, public_data, cache_time=cache_time)
        if getattr(result, "success", False):
            self._last_shared_edit_at[int(widget_id)] = self._time_fn()
        return result

    async def handle_query(self, update: Any) -> None:
        """Answer one tap, whatever happens: never raises, never leaves a tap hanging."""
        query = normalise_widget_query(update)
        if query is None:
            logger.debug("Telegram widget query ignored: not a widget query update")
            return
        result: Optional[str] = None
        lock = self._lock_for(query.widget_id) if query.widget_id is not None else None
        try:
            if lock is None:
                result = await self._invoke_handler(query)
            else:
                async with lock:
                    result = await self._invoke_handler(query)
        except Exception:
            logger.warning("Telegram widget query %s handler failed; answering empty",
                           query.query_id, exc_info=True)
            result = None
        from .widget_messages import validate_widget_result
        try:
            result = validate_widget_result(result)
        except (TypeError, ValueError):
            logger.warning("Telegram widget query %s answer refused; answering empty",
                           query.query_id, exc_info=True)
            result = None
        try:
            await self._adapter.answer_widget_query(query.query_id, result)
        except Exception:
            logger.debug("Telegram widget query %s answer failed",
                         query.query_id, exc_info=True)

    async def _invoke_handler(self, query: WidgetQuery) -> Optional[str]:
        handler = self.handler
        if handler is None:
            return None
        result = await handler(query)
        if result is None:
            return None
        if not isinstance(result, str):
            logger.warning("Telegram widget query %s handler returned %s, not str|None; "
                           "answering empty", query.query_id, type(result).__name__)
            return None
        return result

    def record_widget_data_update(self, update: Any) -> Optional[WidgetState]:
        """Record an UpdateMessageWidgetData in the last-known-state cache.

        Fields the update does not carry keep the cached value: a
        chat_data-only update must not clobber the shared public_data, and
        vice versa. Returns the stored state, or None when the update has
        no peer/message to key on.
        """
        peer = getattr(update, "peer", None)
        msg_id = getattr(update, "msg_id", None)
        if peer is None or msg_id is None:
            return None
        key = (_peer_id(peer), int(msg_id))
        previous = self._states.get(key)
        public_data = getattr(update, "public_data", None)
        public_data_hash = getattr(update, "public_data_hash", None)
        chat_data = getattr(update, "chat_data", None)
        pts = getattr(update, "pts", None)
        if previous is not None:
            if public_data is None:
                public_data = previous.public_data
                public_data_hash = previous.public_data_hash
            if chat_data is None:
                chat_data = previous.chat_data
            if pts is None:
                pts = previous.pts
        state = WidgetState(public_data=public_data, public_data_hash=public_data_hash,
                            chat_data=chat_data, pts=pts)
        self._states[key] = state
        self._states.move_to_end(key)
        while len(self._states) > self._max_states:
            self._states.popitem(last=False)
        return state

    def get_widget_state(self, peer_id: Any, msg_id: Any) -> Optional[WidgetState]:
        """Return the last-known state for one widget message, or None."""
        return self._states.get((peer_id, int(msg_id)))


def get_widget_query_loop(adapter: Any) -> WidgetQueryLoop:
    """Return the adapter's tap loop, creating it on first use."""
    loop = getattr(adapter, "_widget_query_loop", None)
    if loop is None:
        loop = adapter._widget_query_loop = WidgetQueryLoop(adapter)
    return loop


def register_widget_query_handler(adapter: Any, handler: WidgetQueryHandler) -> None:
    """Register the one handler for widget taps (replaces any previous one).

    The handler takes a ``WidgetQuery`` and returns the answer result
    (``None`` = empty answer). It edits state itself, before returning,
    when a tap should change what the widget shows.
    """
    get_widget_query_loop(adapter).handler = handler


def unregister_widget_query_handler(adapter: Any) -> None:
    """Remove the tap handler; taps then get the empty answer."""
    get_widget_query_loop(adapter).handler = None


def get_widget_state(adapter: Any, peer_id: Any, msg_id: Any) -> Optional[WidgetState]:
    """Return the last-known state for one widget message, or None."""
    return get_widget_query_loop(adapter).get_widget_state(peer_id, msg_id)


async def handle_widget_query(adapter: Any, update: Any) -> None:
    """Route one widget tap through the loop; never raises.

    The loop answers every tap itself; this outer guard only covers a
    failure before the loop could (a broken loop or a missing query id),
    and still answers empty when the query id is recoverable.
    """
    try:
        await get_widget_query_loop(adapter).handle_query(update)
    except Exception:
        logger.warning("Telegram widget query dispatch failed; answering empty",
                       exc_info=True)
        query_id = getattr(update, "query_id", None)
        if query_id is not None:
            try:
                await adapter.answer_widget_query(int(query_id))
            except Exception:
                logger.debug("Telegram widget query fallback answer failed", exc_info=True)


async def handle_widget_data_update(adapter: Any, update: Any) -> None:
    """Record one widget state update in the loop's cache; never raises."""
    try:
        get_widget_query_loop(adapter).record_widget_data_update(update)
    except Exception:
        logger.debug("Telegram widget data update was not recorded", exc_info=True)
