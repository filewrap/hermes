"""Per-Telegram-user settings persistence for the Telegram gateway.

SQLite under the active Hermes profile is the zero-configuration default. Set
``TELEGRAM_USER_SETTINGS_MONGODB_URI`` in that profile to use MongoDB instead;
MongoDB support is lazy/optional and never falls back silently if explicitly configured.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict[str, Any] = {
    "name": "",
    "skills": [],
    "mcp_servers": [],
    "mcp_customization_enabled": False,
    "instructions": {"general": "", "search": ""},
    "default_model": "",
    "default_provider": "",
    "memory_enabled": True,
    "self_learning_enabled": True,
    "updated_at": None,
}
_MAX_SKILLS = 40
_MAX_MCPS = 40
_MAX_INSTRUCTION_CHARS = 2_000
_MAX_SETTING_STRING = 200
_SQLITE_LOCK = threading.RLock()
_MONGO_LOCK = threading.RLock()
_MONGO_CLIENTS: dict[str, Any] = {}


def _clean_string(value: Any, limit: int = _MAX_SETTING_STRING) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\x00", "")
    return value[:limit].strip()


def normalize_user_settings(value: Any, *, include_updated_at: bool = True) -> dict[str, Any]:
    """Return a bounded, import-safe document containing only supported preference keys."""
    raw = value if isinstance(value, dict) else {}
    normalized = {
        "name": _clean_string(raw.get("name"), 64),
        "skills": sorted({_clean_string(x) for x in raw.get("skills", []) if isinstance(x, str) and x.strip()})[:_MAX_SKILLS]
        if isinstance(raw.get("skills", []), list) else [],
        "mcp_servers": sorted({_clean_string(x) for x in raw.get("mcp_servers", []) if isinstance(x, str) and x.strip()})[:_MAX_MCPS]
        if isinstance(raw.get("mcp_servers", []), list) else [],
        "mcp_customization_enabled": raw.get("mcp_customization_enabled") is True,
        "instructions": {
            "general": _clean_string((raw.get("instructions") or {}).get("general", ""), _MAX_INSTRUCTION_CHARS)
            if isinstance(raw.get("instructions", {}), dict) else "",
            "search": _clean_string((raw.get("instructions") or {}).get("search", ""), _MAX_INSTRUCTION_CHARS)
            if isinstance(raw.get("instructions", {}), dict) else "",
        },
        "default_model": _clean_string(raw.get("default_model")),
        "default_provider": _clean_string(raw.get("default_provider")),
        "memory_enabled": raw.get("memory_enabled") is not False,
        "self_learning_enabled": raw.get("self_learning_enabled") is not False,
        "updated_at": _clean_string(raw.get("updated_at"), 40) or None,
    }
    if not include_updated_at:
        normalized.pop("updated_at", None)
    return normalized


def _enforce_multiuser_policy(settings: dict[str, Any]) -> None:
    """Per-user provider and custom MCP configuration is disabled in shared mode."""
    if settings.get("default_provider") or settings.get("mcp_servers") or settings.get("mcp_customization_enabled"):
        raise ValueError("Per-user provider and custom MCP configuration is disabled")


def _settings_path(home: str | Path) -> Path:
    return Path(home).expanduser() / "telegram_user_settings.sqlite3"


def _sqlite_load(home: str | Path, user_id: str) -> dict[str, Any]:
    path = _settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    with _SQLITE_LOCK:
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        with sqlite3.connect(str(path), timeout=5.0) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS telegram_user_settings ("
                "user_id TEXT PRIMARY KEY, settings_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT settings_json FROM telegram_user_settings WHERE user_id = ?", (str(user_id),)
            ).fetchone()
    if not row:
        return dict(DEFAULT_SETTINGS)
    try:
        loaded = json.loads(row[0])
    except (TypeError, ValueError):
        logger.warning("Invalid Telegram user settings record; using defaults")
        return dict(DEFAULT_SETTINGS)
    return {**DEFAULT_SETTINGS, **normalize_user_settings(loaded)}


def _sqlite_save(home: str | Path, user_id: str, settings: dict[str, Any]) -> None:
    path = _settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    value = normalize_user_settings({**settings, "updated_at": now})
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    with _SQLITE_LOCK:
        with sqlite3.connect(str(path), timeout=5.0) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS telegram_user_settings ("
                "user_id TEXT PRIMARY KEY, settings_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO telegram_user_settings(user_id, settings_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET settings_json=excluded.settings_json, updated_at=excluded.updated_at",
                (str(user_id), payload, now),
            )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _mongodb_collection(home: str | Path):
    from agent.storage_mode import mongo_primary_enabled
    if not mongo_primary_enabled(home):
        return None
    from gateway.platforms._shared import get_scoped_secret

    uri = str(get_scoped_secret("TELEGRAM_USER_SETTINGS_MONGODB_URI", "") or "").strip()
    if not uri:
        return None
    try:
        from pymongo import MongoClient
    except ImportError as exc:
        raise RuntimeError(
            "MongoDB settings are configured but PyMongo is not installed; install the `mongodb` extra."
        ) from exc
    with _MONGO_LOCK:
        client = _MONGO_CLIENTS.get(uri)
        if client is None:
            # Connection and server-selection limits keep an unavailable DB from stalling chat handling.
            client = MongoClient(uri, appname="Hermes Telegram user settings",
                                 connectTimeoutMS=2500, serverSelectionTimeoutMS=2500)
            _MONGO_CLIENTS[uri] = client
    try:
        db = client.get_default_database(default="hermes")
    except Exception:
        db = client["hermes"]
    namespace = hashlib.sha256(str(Path(home).expanduser().resolve()).encode("utf-8")).hexdigest()[:16]
    return db["telegram_user_settings"], namespace


def load_user_settings(home: str | Path, user_id: str) -> dict[str, Any]:
    """Load settings for one Telegram user within the active profile."""
    user_id = str(user_id or "").strip()
    if not user_id:
        return dict(DEFAULT_SETTINGS)
    from agent.storage_mode import mongo_primary_enabled
    from gateway import mongo_multiuser
    if mongo_primary_enabled(home):
        payload = mongo_multiuser.store_for_home(home).get(
            mongo_multiuser.principal(user_id), "settings", "telegram"
        )
        result = normalize_user_settings(payload if isinstance(payload, dict) else {})
        _enforce_multiuser_policy(result)
        return result
    mongo = _mongodb_collection(home)
    if mongo is None:
        return _sqlite_load(home, user_id)
    collection, namespace = mongo
    document = collection.find_one({"_id": f"{namespace}:{user_id}"})
    if not document or not isinstance(document.get("settings"), dict):
        # Lazy per-user migration keeps switching a profile to MongoDB from losing its local settings.
        legacy = _sqlite_load(home, user_id)
        if legacy.get("updated_at"):
            collection.replace_one(
                {"_id": f"{namespace}:{user_id}"},
                {"_id": f"{namespace}:{user_id}", "settings": legacy}, upsert=True,
            )
        return legacy
    return {**DEFAULT_SETTINGS, **normalize_user_settings(document["settings"])}


def save_user_settings(home: str | Path, user_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Replace a user's settings, retaining only the allowlisted, bounded preference fields."""
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ValueError("A Telegram user ID is required")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    normalized = normalize_user_settings({**(settings or {}), "updated_at": now})
    from agent.storage_mode import mongo_primary_enabled
    from gateway import mongo_multiuser
    if mongo_multiuser.enabled(home):
        _enforce_multiuser_policy(normalized)
    if mongo_primary_enabled(home):
        _enforce_multiuser_policy(normalized)
        mongo_multiuser.store_for_home(home).put(
            mongo_multiuser.principal(user_id), "settings", "telegram", normalized
        )
        return normalized
    mongo = _mongodb_collection(home)
    if mongo is None:
        _sqlite_save(home, user_id, normalized)
    else:
        collection, namespace = mongo
        collection.replace_one(
            {"_id": f"{namespace}:{user_id}"},
            {"_id": f"{namespace}:{user_id}", "settings": normalized}, upsert=True,
        )
    return normalized


def update_user_settings(home: str | Path, user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge preference fields and persist the result."""
    current = load_user_settings(home, user_id)
    if not isinstance(updates, dict):
        raise TypeError("Settings updates must be a mapping")
    merged = {**current, **updates}
    if isinstance(updates.get("instructions"), dict):
        merged["instructions"] = {**current.get("instructions", {}), **updates["instructions"]}
    return save_user_settings(home, user_id, merged)
