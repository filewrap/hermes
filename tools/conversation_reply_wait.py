#!/usr/bin/env python3
"""Correlated-reply waiter (F4).

``conversations_turn`` sends a message into another conversation and waits
for the reply that answers it — not the send receipt. The waiter is a
process-local expectation keyed by the conversation address
``(home, platform, chat_id, thread_id)``; the Telegram adapter's inbound
hook (``_observe_telegram_message``, which already sees only admitted human
messages) calls :func:`notify_inbound` for every message it observes, and
the waiter settles the moment a matching one arrives. The waiter observes a
copy: the inbound message still routes to its own session's turn normally.

Correlation, in order:

- strong — the inbound message replies to the sent message
  (``reply_to_msg_id == sent_message_id``); any chat type.
- weak — the first inbound non-bot message in that conversation after the
  send; DMs only. A group message that merely arrives after ours is not a
  reply to us, so groups and channels need the reply-to link.

Waiters are event-driven (``threading.Event``), never polled: the tool
thread parks in slices so it can touch activity (a parked wait must not
look stale to the delegation heartbeat) and notice an interrupt. A waiter
that is never notified expires at its deadline; the registry never grows
past live waiters plus one expired sweep.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_WAIT_SLICE_S = 0.5


def _home_key(home: Any) -> str:
    from pathlib import Path

    try:
        return str(Path(str(home)).expanduser().resolve())
    except Exception:  # health: allow BLE001 -- an unresolvable home still keys the waiter; it only scopes matching
        return str(home)


def waiter_key(home: Any, platform: str, chat_id: Any, thread_id: Any = None) -> tuple[str, str, str, str]:
    """Address key a waiter registers under and inbound messages match on."""
    return (_home_key(home), str(platform), str(chat_id), str(thread_id or ""))


_waiters_lock = threading.Lock()
# waiter id -> waiter dict. ``message`` is set on settle; ``event`` wakes the
# parked tool thread. Small by construction: notify/register prune expired.
_waiters: dict[str, dict[str, Any]] = {}


def _prune_locked(now: float) -> None:
    for waiter_id, waiter in list(_waiters.items()):
        if waiter.get("event") is not None and waiter["event"].is_set():
            continue  # a settled waiter is reaped by its own await, not here
        if now > float(waiter.get("deadline") or 0):
            _waiters.pop(waiter_id, None)


def register_waiter(
    home: Any, platform: str, chat_id: Any, thread_id: Any = None, *,
    chat_type: Optional[str] = None, sent_message_id: Optional[int] = None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Park one correlated-reply expectation; returns the waiter record."""
    waiter = {
        "id": uuid.uuid4().hex, "key": waiter_key(home, platform, chat_id, thread_id),
        "chat_type": (chat_type or "dm"),
        "sent_message_id": sent_message_id, "sent_at": None,
        "created_at": time.monotonic(), "deadline": time.monotonic() + max(1.0, float(timeout_seconds)),
        "event": threading.Event(), "message": None,
        # Inbound messages seen after registration but before the send was
        # stamped (the reply can beat the stamp on a fast replier): only a
        # reply-to link can prove one of these answers our message, so they
        # are rechecked — never weak-matched — once the sent id is known.
        "pending": [],
    }
    with _waiters_lock:
        _prune_locked(time.monotonic())
        _waiters[waiter["id"]] = waiter
    return waiter


def set_sent_message(waiter: dict[str, Any], message_id: int) -> None:
    """Stamp the sent message id (and send time) the reply correlates to.

    Rechecks messages that arrived before the stamp: a reply-to link to
    this id settles the waiter at once, so a fast reply is never lost to
    the stamp race."""
    waiter["sent_message_id"] = int(message_id)
    waiter["sent_at"] = time.monotonic()
    for candidate in waiter.get("pending") or []:
        if candidate.get("reply_to_msg_id") == int(message_id):
            waiter["message"] = dict(candidate)
            waiter["event"].set()
            break
    waiter["pending"] = []


def cancel_waiter(waiter: dict[str, Any]) -> None:
    """Drop a waiter without settling it (send failed, caller gave up)."""
    with _waiters_lock:
        _waiters.pop(waiter.get("id", ""), None)


def _matches(waiter: dict[str, Any], message: dict[str, Any], now: float) -> bool:
    """Strong reply-to match, else the DM-only next-message match."""
    if message.get("is_bot"):
        return False
    sent_at = waiter.get("sent_at")
    if sent_at is not None and now < sent_at:
        return False
    sent_id = waiter.get("sent_message_id")
    if sent_id is not None and message.get("reply_to_msg_id") == sent_id:
        return True
    if str(waiter.get("chat_type") or "dm") == "dm" and sent_at is not None:
        return True
    return False


def notify_inbound(
    home: Any, platform: str, chat_id: Any, thread_id: Any = None, *,
    message_id: int, sender_id: Any = None, is_bot: bool = False, text: str = "",
    date: Any = None, reply_to_msg_id: Optional[int] = None,
) -> int:
    """Settle every live waiter on this address whose correlation matches.

    Returns how many waiters settled. Never raises: the adapter hook calls
    this for every observed message, and observation must not break on it.
    """
    try:
        key = waiter_key(home, platform, chat_id, thread_id)
        now = time.monotonic()
        message = {
            "message_id": int(message_id), "sender_id": sender_id, "is_bot": bool(is_bot),
            "text": str(text or ""), "date": date, "reply_to_msg_id": reply_to_msg_id,
        }
        settled = 0
        with _waiters_lock:
            _prune_locked(now)
            for waiter in list(_waiters.values()):
                if waiter.get("key") != key or waiter["event"].is_set():
                    continue
                if now > float(waiter.get("deadline") or 0):
                    continue
                if waiter.get("sent_message_id") is None:
                    if not message.get("is_bot"):
                        pending = waiter.setdefault("pending", [])
                        pending.append(dict(message))
                        del pending[:-5]
                    continue
                if not _matches(waiter, message, now):
                    continue
                waiter["message"] = dict(message)
                waiter["event"].set()
                settled += 1
        return settled
    except Exception:  # health: allow BLE001 -- the hook is best-effort; a bad message never breaks observation
        logger.debug("conversation reply waiter: notify failed", exc_info=True)
        return 0


def await_reply(
    waiter: dict[str, Any], *, interrupted: Optional[Callable[[], bool]] = None,
    on_slice: Optional[Callable[[], None]] = None,
) -> tuple[str, Optional[dict[str, Any]]]:
    """Park until the waiter settles, the deadline lapses, or an interrupt.

    Returns ``("replied", message)``, ``("timeout", None)`` or
    ``("interrupted", None)``. ``on_slice`` runs each 0.5s park slice so the
    caller can touch activity; ``interrupted`` is checked on the same slice.
    The waiter is reaped here, whichever way the wait ends.
    """
    event = waiter.get("event")
    try:
        while True:
            if event is not None and event.is_set():
                return "replied", dict(waiter.get("message") or {})
            if interrupted is not None:
                try:
                    if interrupted():
                        return "interrupted", None
                except Exception:  # health: allow BLE001 -- an uncheckable interrupt flag never ends the wait by itself
                    logger.debug("conversation reply waiter: interrupt check failed", exc_info=True)
            remaining = float(waiter.get("deadline") or 0) - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            if event is not None:
                event.wait(timeout=min(_WAIT_SLICE_S, remaining))
            else:
                time.sleep(min(_WAIT_SLICE_S, remaining))
            if on_slice is not None:
                try:
                    on_slice()
                except Exception:  # health: allow BLE001 -- a failed activity touch never ends the wait
                    logger.debug("conversation reply waiter: slice callback failed", exc_info=True)
    finally:
        cancel_waiter(waiter)
