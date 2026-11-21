#!/usr/bin/env python3
"""Conversation-address registry (F4).

The external agent addresses conversations by ``conv_<32-hex>`` refs
(``src/config/sessions/conversation-registry.ts``: ``listConversations``,
``resolveCurrentConversationSession``; ``src/agents/tools/conversation-tools.ts``).
Hermes has no gateway-protocol store to mint random refs in, so refs are
deterministic: ``conv_`` + the first 32 hex chars of
``sha256("<platform>:<chat_id>:<thread_id>")``. A ref survives restarts and
resolves without a live lookup; the same chat always has the same ref.

Durable store: ``<HERMES_HOME>/conversations.json`` (``get_hermes_home()`` at
call time, never a module constant — one process serves many profiles).
Entries record the address only (platform, chat id, thread, type, name,
last seen) — never message content. Live dialogs refresh it
(``conversations_list``); every observed Telegram message refreshes its
chat's row (the ``_observe_telegram_message`` hook).
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Paths resolve lazily: a multiplexed gateway serves several profile homes
# from one process, so an import-time constant would pin every profile to
# whichever home imported first. This global is an explicit override (tests
# patch it); None = current home.
STORE_PATH: Optional[Path] = None

_MAX_ENTRIES = 500
# Re-recording an unchanged address rewrites the whole file; a row whose
# only change is a fresher last_seen inside this window keeps its stored
# stamp. Listing order is unaffected at this resolution.
_LAST_SEEN_WRITE_MIN_SECONDS = 300.0


def conv_ref(platform: str, chat_id: Any, thread_id: Any = None) -> str:
    """Deterministic ``conv_<32-hex>`` ref for one conversation address."""
    key = f"{platform!s}:{chat_id!s}:{thread_id or ''!s}"
    return "conv_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _store_path() -> Path:
    if STORE_PATH is not None:
        return Path(STORE_PATH)
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "conversations.json"


def _load() -> dict[str, Any]:
    from utils import read_json_or_empty

    data = read_json_or_empty(_store_path())
    entries = data.get("conversations") if isinstance(data, dict) else None
    return dict(entries) if isinstance(entries, dict) else {}


def _save(entries: dict[str, Any]) -> None:
    from utils import atomic_json_write

    atomic_json_write(_store_path(), {"version": 1, "conversations": entries})


def _prune(entries: dict[str, Any]) -> None:
    """Drop the stalest rows past the cap (never the just-recorded one)."""
    if len(entries) <= _MAX_ENTRIES:
        return
    ordered = sorted(entries.items(), key=lambda kv: float(kv[1].get("last_seen") or 0))
    for ref, _entry in ordered[: len(entries) - _MAX_ENTRIES]:
        entries.pop(ref, None)


def record_conversation(
    platform: str, chat_id: Any, *, thread_id: Any = None, type: Optional[str] = None,
    name: Optional[str] = None, username: Optional[str] = None,
    last_message_id: Optional[int] = None,
) -> str:
    """Upsert one conversation address; returns its stable ``conv_`` ref."""
    ref = conv_ref(platform, chat_id, thread_id)
    now = time.time()
    entries = _load()
    entry = entries.get(ref)
    if entry is not None and not isinstance(entry, dict):
        entry = None
    if entry is not None:
        changed = False
        for field, value in (
            ("type", type), ("name", name), ("username", username),
            ("last_message_id", last_message_id),
        ):
            if value is not None and entry.get(field) != value:
                entry[field] = value
                changed = True
        if not changed and now - float(entry.get("last_seen") or 0) < _LAST_SEEN_WRITE_MIN_SECONDS:
            return ref
        entry["last_seen"] = now
    else:
        entry = {
            "ref": ref, "platform": str(platform), "chat_id": str(chat_id),
            "thread_id": str(thread_id) if thread_id not in (None, "") else None,
            "type": type, "name": name, "username": username,
            "last_seen": now, "last_message_id": last_message_id,
        }
    entries[ref] = entry
    _prune(entries)
    try:
        _save(entries)
    except Exception:  # health: allow BLE001 -- the registry is an address cache; a failed write never fails the caller
        logger.debug("conversation registry: could not save %s", ref, exc_info=True)
    return ref


def resolve_ref(ref: str) -> Optional[dict[str, Any]]:
    """Stored entry for a ``conv_`` ref, or None when unknown."""
    if not isinstance(ref, str) or not ref.startswith("conv_"):
        return None
    entry = _load().get(ref)
    return dict(entry) if isinstance(entry, dict) else None


def lookup(platform: str, chat_id: Any, thread_id: Any = None) -> Optional[dict[str, Any]]:
    """Stored entry for one address, or None when never recorded."""
    return resolve_ref(conv_ref(platform, chat_id, thread_id))


def list_conversations(
    platform: Optional[str] = None, *, search: Optional[str] = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Stored addresses, freshest first, optionally filtered by platform/name."""
    needle = (search or "").strip().casefold()
    rows = []
    for entry in _load().values():
        if not isinstance(entry, dict):
            continue
        if platform is not None and entry.get("platform") != platform:
            continue
        if needle and needle not in str(entry.get("name") or "").casefold():
            continue
        rows.append(dict(entry))
    rows.sort(key=lambda row: float(row.get("last_seen") or 0), reverse=True)
    return rows[: max(1, int(limit))]
