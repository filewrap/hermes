"""Encrypted, expiring whisper store, one file per allowgc group home.

A whisper is stored as Fernet ciphertext under a random id; the id is the
only thing that ever travels in callback data. Plaintext exists only in
memory while a tap is being handled, never in logs, state files, or
callback payloads. The store is durable across restarts (host-side key
file, 0600) with a 24h TTL and lazy expiry on access — no background
thread: an expired record is dropped when it is read, and expired records
are purged whenever a new whisper is stored.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WHISPER_TTL_SECONDS = 24 * 3600
WHISPER_FILE = "whispers.json"
WHISPER_KEY_FILE = "whisper_key"


def _key_path(home: Path | str) -> Path:
    return Path(home) / WHISPER_KEY_FILE


def _store_path(home: Path | str) -> Path:
    return Path(home) / WHISPER_FILE


def _load_key(home: Path | str) -> bytes:
    """Return the home's Fernet key, creating and storing it (0600) first."""
    from cryptography.fernet import Fernet

    home = Path(home)
    path = _key_path(home)
    try:
        key = path.read_bytes()
        Fernet(key)
        return key
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("Whisper key for home %s is unreadable; refusing to use it", home)
        raise
    key = Fernet.generate_key()
    home.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{WHISPER_KEY_FILE}.", dir=str(home))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return key


def _read_store(home: Path | str) -> dict[str, Any]:
    path = _store_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("Whisper store for home %s is unreadable; treating as empty", home)
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(home: Path | str, data: dict[str, Any]) -> None:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{WHISPER_FILE}.", dir=str(home))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, _store_path(home))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _expires_at(entry: dict[str, Any]) -> int:
    """Expiry epoch of a record; unparseable records count as expired."""
    try:
        return int(entry.get("expires_at", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _purge_expired(data: dict[str, Any], *, now: int) -> dict[str, Any]:
    return {wid: entry for wid, entry in data.items()
            if isinstance(entry, dict) and _expires_at(entry) > now}


def store_whisper(
    home: Path | str, *, sender_id: Any, target_id: Any, text: str,
) -> str:
    """Encrypt ``text`` and store it; return the random id for callback data."""
    from cryptography.fernet import Fernet

    now = int(time.time())
    data = _purge_expired(_read_store(home), now=now)
    wid = secrets.token_hex(16)
    ciphertext = Fernet(_load_key(home)).encrypt(text.encode("utf-8")).decode("ascii")
    data[wid] = {
        "ciphertext": ciphertext,
        "sender_id": str(sender_id),
        "target_id": str(target_id),
        "created_at": now,
        "expires_at": now + WHISPER_TTL_SECONDS,
    }
    _write_store(home, data)
    return wid


def load_whisper(
    home: Path | str, whisper_id: str,
) -> tuple[str, str | None, str | None]:
    """Load a whisper: ``("ok", text, target_id)``, ``("expired", None,
    target_id)`` for a record that was there but lapsed (the target still
    gets "(expired)" on tap), or ``("missing", None, None)``. An expired
    record is dropped on read (lazy expiry)."""
    from cryptography.fernet import Fernet, InvalidToken

    data = _read_store(home)
    entry = data.get(str(whisper_id))
    if not isinstance(entry, dict):
        return "missing", None, None
    now = int(time.time())
    if _expires_at(entry) <= now:
        target_id = str(entry.get("target_id", "") or "")
        data.pop(str(whisper_id), None)
        _write_store(home, data)
        return "expired", None, target_id
    try:
        text = Fernet(_load_key(home)).decrypt(
            str(entry.get("ciphertext", "")).encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        target_id = str(entry.get("target_id", "") or "")
        data.pop(str(whisper_id), None)
        _write_store(home, data)
        return "expired", None, target_id
    return "ok", text, str(entry.get("target_id", "") or "")


def delete_whisper(home: Path | str, whisper_id: str) -> bool:
    """Drop one stored whisper; True when a record was removed."""
    data = _read_store(home)
    if str(whisper_id) not in data:
        return False
    data.pop(str(whisper_id), None)
    _write_store(home, data)
    return True
