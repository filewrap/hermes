"""Private cache of Telegram forum topic IDs known to an owner.

Telegram remains the authority for topic existence and metadata; this catalog only
remembers numeric IDs so callers can resolve them with ``GetForumTopicsByID``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from utils import atomic_json_write, read_json_or_empty

_CATALOG_NAME = ".telegram-known-topic-ids.json"
_MAX_ID = (1 << 63) - 1
_LOCK = threading.RLock()


def _numeric_id(value: Any, *, signed: bool = False) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or (raw[0] == "-" and signed and raw[1:].isdigit()):
            if not (signed and raw.startswith("-") and raw[1:].isdigit()):
                return None
        elif not raw.isdigit():
            return None
        try:
            number = int(raw)
        except ValueError:
            return None
    else:
        return None
    if number == 0 or (number < 0 and not signed) or abs(number) > _MAX_ID:
        return None
    return str(number)


def _scope_key(chat_id: Any, user_id: Any, bot_id: Any) -> str | None:
    chat = _numeric_id(chat_id, signed=True)
    user = _numeric_id(user_id)
    bot = _numeric_id(bot_id)
    if chat is None or user is None or bot is None:
        return None
    return json.dumps([chat, user, bot], separators=(",", ":"))


def _path(home: str | Path) -> Path:
    return Path(home).expanduser().resolve(strict=False) / _CATALOG_NAME


def _read(path: Path) -> dict[str, Any]:
    document = read_json_or_empty(path)
    if document.get("version") != 1 or not isinstance(document.get("topics"), dict):
        return {"version": 1, "topics": {}}
    return document


def _topic_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return sorted({int(item) for item in value
                   if _numeric_id(item) is not None})


def known_topic_ids(home: str | Path, chat_id: Any, user_id: Any, bot_id: Any) -> list[int]:
    """Return known topic IDs only for this resolved profile/chat/user/bot tuple."""
    key = _scope_key(chat_id, user_id, bot_id)
    if key is None:
        return []
    with _LOCK:
        return _topic_ids(_read(_path(home))["topics"].get(key))


def remember_topic(home: str | Path, chat_id: Any, user_id: Any, bot_id: Any, topic_id: Any) -> None:
    """Remember a positive numeric topic ID for the exact owner tuple; invalid IDs are ignored."""
    key = _scope_key(chat_id, user_id, bot_id)
    topic = _numeric_id(topic_id)
    if key is None or topic is None:
        return
    path = _path(home)
    with _LOCK:
        document = _read(path)
        ids = set(_topic_ids(document["topics"].get(key)))
        if int(topic) in ids:
            return
        ids.add(int(topic))
        document["topics"][key] = sorted(ids)
        atomic_json_write(path, document, mode=0o600)


def forget_topic(home: str | Path, chat_id: Any, user_id: Any, bot_id: Any, topic_id: Any) -> None:
    """Forget a topic ID only within the exact owner tuple."""
    key = _scope_key(chat_id, user_id, bot_id)
    topic = _numeric_id(topic_id)
    if key is None or topic is None:
        return
    path = _path(home)
    with _LOCK:
        document = _read(path)
        ids = set(_topic_ids(document["topics"].get(key)))
        ids.discard(int(topic))
        if ids:
            document["topics"][key] = sorted(ids)
        else:
            document["topics"].pop(key, None)
        atomic_json_write(path, document, mode=0o600)
