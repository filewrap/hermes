"""Message edits (beta): edited Telegram messages become new turns.

When ``platforms.telegram.message_edits`` is on for the active profile, an edit
of a user message arrives as a new turn carrying the corrected text (the
adapter marks the event with ``edit_revision`` and the edit metadata the
runner's turn prep reads). The ack ("I saw your edit.") is sent by the adapter
and deletes itself shortly after; the turn itself corrects the previous reply
when the correction changes the answer (see ``gateway/message_edits.py``).

The reply cache below remembers, per (chat, user message), the Telegram id of
the turn response that answered it — the id the final send edits when a later
edit corrects that answer. Only turn responses are recorded (the
``telegram_threaded_reply`` marker the final send sets); status messages,
cards and the ack itself never land here. TTL 600s, 128 entries, same shape
as the threaded-replies chain store: an edit lands seconds after the original,
so a short-lived cache is enough, and a miss just means the correction goes
out as a new message.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

from gateway.message_edits import (
    EDIT_METADATA_CORRECTS_REPLY_ID,
    EDIT_METADATA_IS_EDIT,
    EDIT_METADATA_MESSAGE_ID,
)

from .message_self_delete import DEFAULT_SELF_DELETE_DELAY_S

__all__ = [
    "ACK_SELF_DELETE_S",
    "ACK_TEXT",
    "EDIT_METADATA_CORRECTS_REPLY_ID",
    "EDIT_METADATA_IS_EDIT",
    "EDIT_METADATA_MESSAGE_ID",
    "clear_reply",
    "edit_revision_for",
    "message_edits_enabled",
    "previous_reply_id_for",
    "record_reply",
]

_CONFIG_KEY = "message_edits"
_RUNTIME_ATTR = "_message_edits_runtime"
_REPLY_TTL_S = 600.0
_REPLY_MAX_ENTRIES = 128

#: The ack the adapter sends for an admitted edit (deleted shortly after).
ACK_TEXT = "I saw your edit."
#: The ack deletes itself after this many seconds (the transient-message default).
ACK_SELF_DELETE_S = DEFAULT_SELF_DELETE_DELAY_S


def message_edits_enabled(adapter: Any) -> bool:
    """Profile-wide ``platforms.telegram.message_edits``; False when unset.

    The live runtime attr (set by the /settings toggle) wins over the
    persisted config value, exactly like the threaded-replies reader.
    """
    runtime = getattr(adapter, _RUNTIME_ATTR, None)
    if runtime is not None:
        return bool(runtime)
    extra = getattr(getattr(adapter, "config", None), "extra", None)
    if isinstance(extra, dict):
        return bool(extra.get(_CONFIG_KEY, False))
    return False


def _reply_store(adapter: Any) -> dict:
    store = getattr(adapter, "_message_edit_replies", None)
    if not isinstance(store, dict):
        store = {}
        adapter._message_edit_replies = store
    return store


def _prune_reply_store(store: dict) -> None:
    now = time.monotonic()
    for key in [key for key, (_, stamp) in store.items() if now - stamp > _REPLY_TTL_S]:
        del store[key]
    while len(store) > _REPLY_MAX_ENTRIES:
        del store[min(store, key=lambda key: store[key][1])]


def record_reply(adapter: Any, chat_id: Any, user_message_id: Any, reply_message_id: Any) -> None:
    """Remember a turn response's id as the reply to (chat, user message)."""
    if not user_message_id or not reply_message_id:
        return
    store = _reply_store(adapter)
    store[(str(chat_id), str(user_message_id))] = (str(reply_message_id), time.monotonic())
    _prune_reply_store(store)


def clear_reply(adapter: Any, chat_id: Any, user_message_id: Any) -> None:
    """Drop the recorded reply for (chat, user message); a failed send has none."""
    if not user_message_id:
        return
    _reply_store(adapter).pop((str(chat_id), str(user_message_id)), None)


def previous_reply_id_for(adapter: Any, chat_id: Any, user_message_id: Any) -> Optional[str]:
    """The recorded reply id for (chat, user message), or None when cold."""
    if not user_message_id:
        return None
    store = _reply_store(adapter)
    entry = store.get((str(chat_id), str(user_message_id)))
    if entry is None:
        return None
    reply_id, stamp = entry
    if time.monotonic() - stamp > _REPLY_TTL_S:
        del store[(str(chat_id), str(user_message_id))]
        return None
    return str(reply_id)


def edit_revision_for(message: Any) -> str:
    """Identity of one edit: ``"<message id>:<edit epoch>"``.

    Telethon's MessageEdited event keeps only the edited message (no update
    id), so the revision is derived from the message id and its edit date —
    each edit bumps the date, so each revision is unique. Falls back to the
    current time when the message carries no edit date.
    """
    message_id = str(getattr(message, "id", "") or "")
    edit_date = getattr(message, "edit_date", None)
    stamp = None
    if isinstance(edit_date, datetime):
        try:
            stamp = int(edit_date.timestamp())
        except (ValueError, OverflowError, OSError):
            stamp = None
    if stamp is None:
        stamp = int(time.time())
    return f"{message_id}:{stamp}"
