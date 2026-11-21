"""Numeric codes exchanged between Hermes peers, and the talk lifecycle.

Codes are exact-message matches: a message that is only the code, except the
leave code, which carries a chat id as its argument. A code sent to anything
that is not a Hermes peer is plain text and does nothing.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from gateway.session import _session_key_namespace

from .bot_operations import (
    BotOperationsError,
    TelegramBotOperations,
    _PEER_SETTING_DEFAULTS,
    _redact_external_text,
)

logger = logging.getLogger(__name__)

CODE_GREETING = 3003
CODE_ERROR = 3077
CODE_SECURE = 3039
CODE_LEAVE = 3083

_CODE_RE = re.compile(r"^(\d{4})(?:\s+(\S+))?$")
_CHAT_ID_RE = re.compile(r"^-?\d+$")
_MENTION_PREFIX_RE = re.compile(r"^@\S+\s+")

# One-shot note, armed when a talk is stopped: the next turn on the talk's
# session reads the old talk as closed instead of continuing it from history.
TALK_CLOSED_TEXT = (
    "The previous talk with this peer was stopped by the owner. "
    "Its task is closed — do not continue it. "
    "Treat the peer's new message as a fresh request."
)

_NOTES_FILENAME = "b2b_talk_notes.json"
_NOTIFY_THROTTLE_SECONDS = 60.0
_notify_last: dict[tuple[str, int], float] = {}


def talk_session_key(profile: str | None, peer_id: int | str) -> str:
    """Per-peer session key: one isolated transcript + model override per peer."""
    return f"{_session_key_namespace(profile)}:telegram:b2b:{int(peer_id)}"


def peer_settings(peer: dict[str, Any] | None) -> dict[str, Any]:
    """Peer settings with the defaults filled in for missing keys."""
    settings = (peer or {}).get("settings")
    if not isinstance(settings, dict):
        settings = {}
    return {**_PEER_SETTING_DEFAULTS, **settings}


def live_talk_record(peer_id: int | str, *, model: Any = None,
                     last_message: str = "", started_at: Any = None) -> dict[str, Any]:
    """Talk record shape for ``upsert_talk``; the store fills missing times."""
    return {
        "peer_id": int(peer_id),
        "state": "live",
        "started_at": started_at,
        "last_at": None,
        "last_message": str(last_message or ""),
        "model": model,
        "killed": False,
    }


def parse_code(text: Any, *, bot_username: str | None = None) -> tuple[int, str | None] | None:
    """Exact-message match for a code, else None.

    A leading mention of this bot is stripped first (group sends address the
    recipient that way). The leave code without a chat id parses as a failed
    leave; anything with extra words is plain text.
    """
    raw = str(text or "").strip()
    name = str(bot_username or "").lstrip("@")
    if name:
        match = re.match(rf"^@{re.escape(name)}\s+", raw, re.IGNORECASE)
        if match:
            raw = raw[match.end():].strip()
    match = _CODE_RE.match(raw)
    if not match:
        return None
    code = int(match.group(1))
    arg = match.group(2)
    if code == CODE_LEAVE:
        if arg is None:
            return (code, None)
        return (code, arg) if _CHAT_ID_RE.match(arg) else None
    if arg is not None:
        return None
    if code in (CODE_GREETING, CODE_ERROR, CODE_SECURE):
        return (code, None)
    return None


def evaluate_peer_trigger(mode: str, *, mentioned: bool, reply_to_own: bool,
                          word_trigger: bool) -> bool:
    """Whether a group message addresses this bot under the peer's trigger."""
    if mode == "direct":
        return True
    if mentioned or reply_to_own:
        return True
    return mode == "word" and word_trigger


# --- one-shot closed notes ---------------------------------------------------


def _notes_path(home: str | Path) -> Path:
    return Path(home) / "telegram" / _NOTES_FILENAME


def _read_notes(home: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(_notes_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_notes(home: str | Path, notes: dict[str, Any]) -> None:
    path = _notes_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(notes, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def arm_talk_closed_note(home: str | Path, session_key: str, peer_id: int | str) -> None:
    """Arm a one-shot closed note for the next turn on *session_key*."""
    try:
        notes = _read_notes(home)
        notes[str(session_key)] = {"peer_id": int(peer_id), "text": TALK_CLOSED_TEXT}
        _write_notes(home, notes)
    except (OSError, ValueError):
        logger.debug("Talk closed-note arm failed", exc_info=True)


def take_talk_closed_note(home: str | Path, session_key: str) -> dict[str, Any] | None:
    """Consume the pending closed note for *session_key*, if any."""
    try:
        notes = _read_notes(home)
        note = notes.pop(str(session_key), None)
        if note is not None:
            _write_notes(home, notes)
        return note if isinstance(note, dict) else None
    except (OSError, ValueError):
        return None


# --- inbound routing ---------------------------------------------------------


async def route_peer_message(adapter: Any, event: Any, source: Any,
                             text: str) -> tuple[bool, str]:
    """Route one admitted peer-bot message.

    Returns ``(route_to_turn, closed_note_text)``. Codes are handled first,
    then the peer's intake setting; anything else is handled here and returns
    ``(False, "")``. Unadmitted senders are dropped silently.
    """
    operations = TelegramBotOperations(adapter)
    if not operations.admission_check(source):
        return False, ""
    try:
        peer_id = int(source.user_id)
    except (TypeError, ValueError):
        return False, ""
    try:
        store = operations._store(source)
        peer = store.peer(peer_id)
    except (BotOperationsError, OSError, ValueError):
        return False, ""
    if peer is None:
        return False, ""
    settings = peer_settings(peer)
    code = parse_code(text, bot_username=getattr(adapter, "_bot_username", None))
    if code is not None:
        await _handle_code(adapter, operations, store, peer, settings, source, event, code)
        return False, ""
    if settings.get("intake") == "inbox":
        await operations.handle_incoming(event, source)
        return False, ""
    if str(getattr(source, "chat_type", "")) in {"group", "forum", "channel"}:
        if not await _peer_trigger_passes(adapter, event, source, settings):
            return False, ""
    if settings.get("acceptance") == "short":
        await _send_peer_text(adapter, source, event,
                              str(settings.get("acceptance_text") or "accepted ok"))
        _note_incoming(store, peer_id, text)
        return False, ""
    _note_incoming(store, peer_id, text)
    talk_key = talk_session_key(session_key_profile(adapter, source), peer_id)
    await _apply_peer_model(adapter, talk_key, settings.get("model"))
    live = _get_talk(store, peer_id)
    secure = bool(live and live.get("state") == "live" and not live.get("killed"))
    if secure:
        _refresh_live_talk(store, peer_id, live, settings, text)
    event._b2b_admitted = True
    event._b2b_secure = secure
    try:
        source.b2b_session_key = talk_key
    except Exception:
        logger.debug("Could not pin the talk session key", exc_info=True)
    await _maybe_notify_owner(adapter, operations, peer, source)
    note = None
    try:
        note = take_talk_closed_note(operations.profile_home(source), talk_key)
    except Exception:
        logger.debug("Talk closed-note take failed", exc_info=True)
        note = None
    return True, str((note or {}).get("text") or "")


async def _handle_code(adapter: Any, operations: TelegramBotOperations, store: Any,
                       peer: dict[str, Any], settings: dict[str, Any], source: Any,
                       event: Any, code: tuple[int, str | None]) -> None:
    number, arg = code
    peer_id = int(peer["id"])
    if number == CODE_GREETING:
        await _send_code(adapter, peer, source, event, CODE_GREETING)
    elif number == CODE_ERROR:
        await _send_code(adapter, peer, source, event, CODE_ERROR)
    elif number == CODE_SECURE:
        if settings.get("secure") != "allowed":
            return
        existing = _get_talk(store, peer_id)
        if existing and existing.get("killed"):
            return
        try:
            store.upsert_talk(peer_id, live_talk_record(
                peer_id, model=settings.get("model"),
                started_at=(existing or {}).get("started_at")
                if existing and existing.get("state") == "live" else None))
        except (BotOperationsError, OSError, ValueError):
            return
    elif number == CODE_LEAVE:
        if arg is None:
            return
        existing = _get_talk(store, peer_id)
        if not existing or existing.get("state") != "live":
            return
        stopped = dict(existing, state="stopped", killed=False, last_at=int(time.time()))
        try:
            store.upsert_talk(peer_id, stopped)
        except (BotOperationsError, OSError, ValueError):
            return
        await _interrupt_talk(adapter, source, peer_id, reason="b2b_leave")
        try:
            home = operations.profile_home(source)
            arm_talk_closed_note(
                home, talk_session_key(session_key_profile(adapter, source), peer_id), peer_id)
        except Exception:
            logger.debug("Talk closed-note arm failed", exc_info=True)


async def _send_code(adapter: Any, peer: dict[str, Any], source: Any,
                     event: Any, number: int) -> None:
    text = str(number)
    if str(getattr(source, "chat_type", "")) in {"group", "forum", "channel"}:
        username = str(peer.get("username") or "").lstrip("@")
        if username:
            text = f"@{username} {text}"
    message_id = getattr(getattr(event, "message", None), "id", None)
    try:
        await adapter.send(str(source.chat_id), text,
                           reply_to=message_id if isinstance(message_id, int) else None)
    except Exception:
        logger.debug("Code reply failed", exc_info=True)


async def _send_peer_text(adapter: Any, source: Any, event: Any, text: str) -> None:
    message_id = getattr(getattr(event, "message", None), "id", None)
    try:
        await adapter.send(str(source.chat_id), text,
                           reply_to=message_id if isinstance(message_id, int) else None)
    except Exception:
        logger.debug("Peer text reply failed", exc_info=True)


async def _peer_trigger_passes(adapter: Any, event: Any, source: Any,
                               settings: dict[str, Any]) -> bool:
    mode = str(settings.get("trigger") or "mention")
    if mode == "direct":
        return True
    raw = str(getattr(getattr(event, "message", None), "message", "")
              or getattr(event, "raw_text", "") or "")
    username = str(getattr(adapter, "_bot_username", "") or "").lstrip("@").lower()
    mentioned = bool(username and re.search(
        rf"(?<![\w])@{re.escape(username)}(?![\w])", raw, re.IGNORECASE))
    if not mentioned and getattr(adapter, "_bot_id", None) is not None:
        for entity in (getattr(getattr(event, "message", None), "entities", None) or []):
            if type(entity).__name__ not in {"MessageEntityMentionName",
                                             "MessageEntityInputMentionName"}:
                continue
            target = getattr(entity, "user_id", None)
            target_id = getattr(target, "user_id", target)
            try:
                if int(target_id) == int(adapter._bot_id):
                    mentioned = True
                    break
            except (TypeError, ValueError):
                continue
    reply_to_own = False
    try:
        reply_to_own = bool(await adapter._reply_to_own_message(event))
    except Exception:
        logger.debug("Peer reply-to-own check failed", exc_info=True)
        reply_to_own = False
    word_trigger = False
    if mode == "word":
        try:
            chat_settings = adapter._chat_settings(source) or {}
        except Exception:
            logger.debug("Peer trigger-word read failed", exc_info=True)
            chat_settings = {}
        word = str(chat_settings.get("trigger_word") or "").strip()
        word_trigger = bool(word and re.search(
            rf"(?<!\w){re.escape(word)}(?!\w)", raw, re.IGNORECASE))
    return evaluate_peer_trigger(mode, mentioned=mentioned,
                                 reply_to_own=reply_to_own, word_trigger=word_trigger)


def _get_talk(store: Any, peer_id: int) -> dict[str, Any] | None:
    """Best-effort talk read; a store failure reads as 'no live talk'."""
    try:
        return store.get_talk(peer_id)
    except (BotOperationsError, OSError, ValueError):
        return None


def _note_incoming(store: Any, peer_id: int, text: str) -> None:
    try:
        store.note_peer_incoming(peer_id, text)
    except (BotOperationsError, OSError, ValueError):
        pass


def _refresh_live_talk(store: Any, peer_id: int, live: dict[str, Any],
                       settings: dict[str, Any], text: str) -> None:
    try:
        store.upsert_talk(peer_id, live_talk_record(
            peer_id, model=settings.get("model"),
            last_message=_redact_external_text(text)[:1000],
            started_at=live.get("started_at")))
    except (BotOperationsError, OSError, ValueError):
        pass


async def _apply_peer_model(adapter: Any, talk_key: str, model: Any) -> None:
    runner = getattr(adapter, "gateway_runner", None)
    store = getattr(runner, "async_session_store", None) if runner is not None else None
    if store is None:
        return
    override = {"model": str(model)} if model else None
    try:
        await store.set_model_override(talk_key, override)
    except Exception:
        logger.debug("Peer model override failed", exc_info=True)
        return
    overrides = getattr(runner, "_session_model_overrides", None)
    try:
        if override is None:
            overrides.pop(talk_key, None)
        else:
            overrides[talk_key] = dict(override)
    except Exception:
        logger.debug("Peer model override map update failed", exc_info=True)


async def _maybe_notify_owner(adapter: Any, operations: TelegramBotOperations,
                              peer: dict[str, Any], source: Any) -> None:
    settings = peer_settings(peer)
    if settings.get("notify") != "on":
        return
    try:
        home = operations.profile_home(source)
    except (BotOperationsError, OSError, ValueError):
        return
    key = (str(home), int(peer["id"]))
    now = time.monotonic()
    if now - _notify_last.get(key, 0.0) < _NOTIFY_THROTTLE_SECONDS:
        return
    _notify_last[key] = now
    for old_key, old_at in list(_notify_last.items()):
        if now - old_at >= _NOTIFY_THROTTLE_SECONDS:
            _notify_last.pop(old_key, None)
    try:
        from gateway.mongo_multiuser import owner_user_id
        owner = owner_user_id(home)
    except Exception:
        logger.debug("Peer owner lookup failed", exc_info=True)
        return
    if not owner:
        return
    name = str(peer.get("name") or peer.get("username") or peer["id"])
    try:
        await adapter.send(str(owner), f"Peer {name} wrote in {source.chat_id}.")
    except Exception:
        logger.debug("Peer notify failed", exc_info=True)


async def _interrupt_talk(adapter: Any, source: Any, peer_id: int, *, reason: str) -> None:
    runner = getattr(adapter, "gateway_runner", None)
    interrupt = getattr(runner, "_interrupt_and_clear_session", None) if runner is not None else None
    if not callable(interrupt):
        return
    talk_key = talk_session_key(session_key_profile(adapter, source), peer_id)
    try:
        await interrupt(talk_key, source, interrupt_reason=reason, invalidation_reason=reason)
    except Exception:
        logger.debug("Talk interrupt failed", exc_info=True)


def session_key_profile(adapter: Any, source: Any) -> str | None:
    fn = getattr(adapter, "_session_key_profile", None)
    if not callable(fn):
        return None
    try:
        profile = fn(source)
    except Exception:
        logger.debug("Talk session profile lookup failed", exc_info=True)
        return None
    return profile if isinstance(profile, str) else None


# --- sender side -------------------------------------------------------------


async def note_sent_code(adapter: Any, chat_id: Any, text: str) -> None:
    """Mark our own talk live when this side sends the agreement code."""
    stripped = str(text or "").strip()
    stripped = _MENTION_PREFIX_RE.sub("", stripped, count=1).strip()
    if stripped != str(CODE_SECURE):
        return
    home = getattr(adapter, "_telegram_transport_home", None)
    if home is None:
        return
    try:
        from .bot_operations import BotOperationsStore
        store = BotOperationsStore(home)
        peer = _resolve_send_peer(store, str(chat_id))
        if peer is None:
            return
        settings = peer_settings(peer)
        if settings.get("secure") != "allowed":
            return
        existing = store.get_talk(peer["id"])
        if existing and existing.get("killed"):
            return
        store.upsert_talk(peer["id"], live_talk_record(
            peer["id"], model=settings.get("model"),
            started_at=(existing or {}).get("started_at")
            if existing and existing.get("state") == "live" else None))
    except (BotOperationsError, OSError, ValueError):
        return


def _resolve_send_peer(store: Any, chat_id: str) -> dict[str, Any] | None:
    peer = store.peer(chat_id)
    if peer and peer.get("dm_enabled"):
        return peer
    admitted = [entry for entry in store.peers()
                if chat_id in set(map(str, entry.get("group_chat_ids", [])))]
    return admitted[0] if len(admitted) == 1 else None
