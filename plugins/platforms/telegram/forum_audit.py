"""Durable, profile-scoped outbox for the optional private Telegram forum audit.

This module never connects to Telegram on enqueue. The Telethon adapter owns the
transport callbacks and should invoke ``drain`` from a background worker. Configuration
lives under ``telegram.extra.forum_audit`` and is inert unless explicitly enabled.
"""
from __future__ import annotations

import hashlib
import logging
import math
import sqlite3
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_DB_NAME = ".telegram-forum-audit.sqlite3"
_MAX_ROWS = 5000
_MAX_BYTES = 16 * 1024 * 1024
_MAX_TEXT = 16_000
_MAX_RETRIES = 20
_LOCK = threading.RLock()
_SERVICES: dict[str, ForumAuditLogger] = {}
_SERVICE_CANDIDATES: dict[str, list[tuple[int, weakref.ReferenceType[ForumAuditLogger]]]] = {}
_SERVICE_SEQUENCE = 0
_DRAIN_LOCKS: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()


def _numeric(value: Any, *, signed: bool = False) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if result == 0 or abs(result) >= (1 << 63) or (result < 0 and not signed):
        return None
    return result


def _profile_root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve(strict=False)


def _storage_root(root: str | Path, forum_id: int | None,
                  transport_id: str | None = None) -> Path:
    root = _profile_root(root)
    profile_id = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    storage = (root / ".telegram-forum-audit" / profile_id / str(forum_id)
               if forum_id is not None else root / ".telegram-forum-audit-disabled")
    if transport_id is not None:
        storage = storage / hashlib.sha256(transport_id.encode("utf-8")).hexdigest()
    return storage


def _db_path(root: str | Path) -> Path:
    requested = Path(root).expanduser()
    if requested.is_symlink() or requested.parent.is_symlink():
        raise OSError("Telegram audit storage path must not traverse symlinks")
    directory = _profile_root(requested)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / _DB_NAME


def _database(root: str | Path) -> sqlite3.Connection:
    path = _db_path(root)
    if path.is_symlink():
        raise OSError("Telegram audit database must not be a symlink")
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        path.chmod(0o600)
    except OSError:
        conn.close()
        raise
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute('PRAGMA max_page_count=16384')  # 64 MiB with SQLite's 4096-byte pages.
    conn.execute('PRAGMA journal_size_limit=4194304')
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY,
            dedupe_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL CHECK(kind IN ('incoming','general','operations')),
            user_id INTEGER,
            source_chat_id INTEGER,
            source_message_id INTEGER,
            topic_id INTEGER,
            chat_type TEXT NOT NULL DEFAULT 'unknown',
            thread_id TEXT,
            payload TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS outbox_due ON outbox(next_attempt, id);
        CREATE TABLE IF NOT EXISTS receipts (
            dedupe_key TEXT PRIMARY KEY,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_topics (
            user_id INTEGER PRIMARY KEY,
            topic_id INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS unique_user_topic ON user_topics(topic_id);
        CREATE TABLE IF NOT EXISTS forum_topics (
            topic_key TEXT PRIMARY KEY,
            random_id INTEGER NOT NULL UNIQUE,
            topic_id INTEGER UNIQUE
        );
        CREATE TABLE IF NOT EXISTS transport_state (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL
        );
    """)
    # Legacy/queued non-DM transcripts must be removed even during cooldown,
    # before any topic resolution or transport work. Never copy their metadata
    # or content into Operations, or reinterpret their origin as a private chat.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    if "chat_type" not in columns:
        conn.execute("ALTER TABLE outbox ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'unknown'")
    if "thread_id" not in columns:
        conn.execute("ALTER TABLE outbox ADD COLUMN thread_id TEXT")
    conn.create_function("is_private_transcript", 3, is_private_transcript, deterministic=True)
    conn.execute("""DELETE FROM receipts WHERE dedupe_key IN
        (SELECT dedupe_key FROM outbox WHERE kind='incoming' AND
         NOT is_private_transcript(chat_type,source_chat_id,thread_id))""")
    removed = conn.execute("""DELETE FROM outbox WHERE kind='incoming' AND
        NOT is_private_transcript(chat_type,source_chat_id,thread_id)""").rowcount
    if removed:
        logger.warning("Telegram forum audit discarded non-private or unverified queued transcripts")
    conn.commit()
    for suffix in ("", "-wal", "-shm"):
        artifact = Path(str(path) + suffix)
        if artifact.exists():
            if artifact.is_symlink():
                conn.close()
                raise OSError("Telegram audit database artifacts must not be symlinks")
            artifact.chmod(0o600)
    return conn


@contextmanager
def _db(root: str | Path):
    conn = _database(root)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _bounded_text(text: Any) -> str:
    value = str(text).strip()
    return value[:_MAX_TEXT]


_NO_PEER = object()


def is_private_transcript(chat_type: Any, source_chat_id: Any,
                          thread_id: Any = None, *, peer: Any = _NO_PEER) -> bool:
    """DM-only audit boundary; unknown or contradictory origin scope is denied.

    Ingress supplies Telegram's actual message peer, not a mention/reply trigger.
    Persisted rows retain that admitted scope; delivery verifies the resolved peer
    again. Positive IDs alone are never sufficient evidence of a private chat.
    """
    source = _numeric(source_chat_id)
    if (not isinstance(chat_type, str) or chat_type not in {"dm", "private", "direct"}
            or source is None or (thread_id is not None and _numeric(thread_id) is None)):
        return False
    if peer is _NO_PEER:
        return True
    from telethon import types
    if isinstance(peer, types.User):
        return peer.id == source
    return isinstance(peer, (types.PeerUser, types.InputPeerUser)) and peer.user_id == source


def _insert(root: str | Path, *, key: str, kind: str, payload: str = "",
            user_id: int | None = None, source_chat_id: int | None = None,
            source_message_id: int | None = None, topic_id: int | None = None,
            chat_type: str = "unknown", thread_id: str | None = None) -> bool:
    if kind == "incoming" and not is_private_transcript(chat_type, source_chat_id, thread_id):
        return False
    if not payload and kind != "incoming":
        return False
    if len(key) > 256:
        key = 'sha256:' + hashlib.sha256(key.encode()).hexdigest()
    try:
        with _LOCK, _db(root) as db:
            now = time.time()
            db.execute("DELETE FROM receipts WHERE created_at<?", (now - 30 * 86400,))
            if db.execute("SELECT 1 FROM receipts WHERE dedupe_key=?", (key,)).fetchone():
                return False
            if db.execute("SELECT count(*) FROM receipts").fetchone()[0] >= 50_000:
                logger.warning("Telegram forum audit dedupe store full; event not queued")
                return False
            rows, size = db.execute("SELECT count(*), coalesce(sum(length(CAST(payload AS BLOB))),0) FROM outbox").fetchone()
            if rows >= _MAX_ROWS or size + len(payload.encode("utf-8")) > _MAX_BYTES:
                logger.warning("Telegram forum audit outbox full for profile %s; event not queued", _profile_root(root).name)
                return False
            db.execute("""INSERT INTO outbox
                (dedupe_key,kind,user_id,source_chat_id,source_message_id,topic_id,chat_type,thread_id,payload,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (key, kind, user_id, source_chat_id, source_message_id, topic_id,
                 chat_type, thread_id, payload, now))
            db.execute("INSERT INTO receipts(dedupe_key,created_at) VALUES(?,?)", (key, now))
            return True
    except (OSError, sqlite3.Error):
        logger.warning("Could not durably enqueue Telegram forum audit event for profile %s",
                       _profile_root(root).name, exc_info=True)
        return False


class ForumAuditLogger:
    """Per-profile SQLite outbox; outbound effects are only performed by ``drain``."""

    def __init__(self, root: str | Path, config: dict[str, Any] | None = None,
                 *, transport_id: str | None = None):
        self.root = _profile_root(root)
        self.transport_id = transport_id
        self.config = dict(config or {}) if isinstance(config, dict) else {}
        self.forum_id = _numeric(self.config.get("chat_id"), signed=True)
        self.general_topic_id = _numeric(self.config.get("general_topic_id"))
        self.operations_topic_id = _numeric(self.config.get("operations_topic_id"))
        verified = all(self.config.get(key) is True for key in (
            "verified_entity", "verified_forum", "verified_rights"))
        self.enabled = bool(self.config.get("enabled") is True and verified and self.forum_id
                            and self.forum_id < 0 and self.general_topic_id == 1
                            and self.operations_topic_id and self.operations_topic_id > 1)
        # A copied/moved profile must not reuse the old profile's pending events
        # or user-topic IDs. Each destination is also isolated on reconfiguration.
        self._storage_root = _storage_root(self.root, self.forum_id, transport_id)
        self._drain_lock = _drain_lock_for(self._storage_root)
        with _LOCK:
            global _SERVICE_SEQUENCE
            _SERVICE_SEQUENCE += 1
            profile = str(self.root)
            _SERVICE_CANDIDATES.setdefault(profile, []).append((_SERVICE_SEQUENCE, weakref.ref(self)))
            _refresh_service(profile)

    def status(self) -> dict[str, Any]:
        """Return safe setup status; never include credentials or user content."""
        if not self.enabled:
            return {"configured": False, "enabled": False,
                    "message": "Forum audit is disabled or missing numeric forum/topic IDs."}
        try:
            with _db(self._storage_root) as db:
                pending = db.execute("SELECT count(*) FROM outbox").fetchone()[0]
                users = db.execute("SELECT count(*) FROM user_topics").fetchone()[0]
        except (OSError, sqlite3.Error):
            pending, users = 0, 0
        return {"configured": True, "enabled": True, "forum_id": self.forum_id,
                "pending": pending, "mapped_users": users}

    def enqueue_general(self, text: str, *, key: str) -> bool:
        if not self.enabled:
            logger.warning("Telegram forum audit General notice skipped: not configured")
            return False
        notice_key = str(key)
        if not notice_key.startswith("error:"):
            logger.warning("General forum audit is restricted to error notices")
            return False
        return _insert(self._storage_root, key="general:" + notice_key, kind="general",
                       payload=_bounded_text(text), topic_id=self.general_topic_id)

    def enqueue_inbound(self, *, user_id: Any, source_chat_id: Any, message_id: Any,
                        authorized: bool, guest: bool = False, chat_type: str = "unknown",
                        thread_id: Any = None) -> bool:
        """Inbound user messages are never queued for the audit forum.

        Hermes does not store or forward user message content to the logs
        channel; the audit forum carries operational error notices only.
        """
        return False

    def flood_until(self) -> float:
        """Return the durable transport cooldown timestamp, or zero if unset."""
        try:
            with _db(self._storage_root) as db:
                row = db.execute("SELECT value FROM transport_state WHERE key='flood_until'").fetchone()
            value = float(row[0]) if row else 0.0
            return value if math.isfinite(value) else 0.0
        except (OSError, sqlite3.Error, TypeError, ValueError):
            logger.warning("Could not read Telegram forum audit cooldown", exc_info=True)
            return 0.0

    def defer_flood(self, until: float) -> None:
        """Persist a shared-adapter cooldown, extending but never shortening it."""
        try:
            timestamp = float(until)
            if not math.isfinite(timestamp):
                return
            with _LOCK, _db(self._storage_root) as db:
                db.execute("INSERT INTO transport_state VALUES('flood_until',?) ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)",
                           (timestamp,))
        except (OSError, sqlite3.Error, TypeError, ValueError):
            logger.warning("Could not persist Telegram forum audit cooldown", exc_info=True)

    def set_user_topic(self, user_id: Any, topic_id: Any) -> bool:
        """Persist numeric per-profile uid→topic mapping after verified topic creation."""
        uid, topic = _numeric(user_id), _numeric(topic_id)
        if not self.enabled or uid is None or topic is None or topic in {1, self.general_topic_id, self.operations_topic_id}:
            return False
        try:
            with _LOCK, _db(self._storage_root) as db:
                existing = db.execute("SELECT 1 FROM user_topics WHERE user_id=?", (uid,)).fetchone()
                if not existing and db.execute("SELECT count(*) FROM user_topics").fetchone()[0] >= _MAX_ROWS:
                    logger.warning("Telegram forum audit user-topic mapping store is full")
                    return False
                db.execute("INSERT INTO user_topics VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET topic_id=excluded.topic_id,updated_at=excluded.updated_at",
                           (uid, topic, time.time()))
            return True
        except (OSError, sqlite3.Error):
            logger.warning("Could not save Telegram forum audit topic mapping", exc_info=True)
            return False

    def user_topic(self, user_id: Any, *, strict: bool = False) -> int | None:
        uid = _numeric(user_id)
        if uid is None:
            return None
        try:
            with _db(self._storage_root) as db:
                row = db.execute("SELECT topic_id FROM user_topics WHERE user_id=?", (uid,)).fetchone()
            topic = int(row[0]) if row else None
            if row and (topic <= 1 or topic in {self.general_topic_id, self.operations_topic_id}):
                if strict:
                    raise RuntimeError("invalid saved per-user topic ID")
                return None
            return topic
        except (OSError, sqlite3.Error):
            logger.warning("Could not read Telegram forum audit topic mapping", exc_info=True)
            if strict:
                raise
            return None

    async def drain(self, *, send_text: Callable[[int, int, str, str], Awaitable[Any]],
                    limit: int = 25) -> int:
        """Attempt due rows; caller runs this in a background task, never inbound."""
        if not self.enabled:
            return 0
        async with self._drain_lock:
            if not self.enabled:
                return 0
            if self.flood_until() > time.time():
                return 0
            with _LOCK, _db(self._storage_root) as db:
                rows = db.execute("SELECT id,kind,user_id,source_chat_id,source_message_id,topic_id,payload,attempts,dedupe_key,chat_type,thread_id FROM outbox WHERE next_attempt<=? ORDER BY id LIMIT ?",
                                  (time.time(), max(1, min(int(limit), 100)))).fetchall()
            completed = 0
            for row in rows:
                if not self.enabled:
                    break
                (row_id, kind, _uid, _source, _msg, _topic, payload, attempts, event_key,
                 _chat_type, _thread_id) = row
                try:
                    if kind == "incoming":
                        # Inbound forwarding is disabled: user messages are
                        # never forwarded to the audit forum. Drop rows queued
                        # before the change without a trace.
                        logger.warning(
                            "Telegram forum audit dropped a queued inbound message row; "
                            "user messages are never forwarded to the audit forum")
                        with _LOCK, _db(self._storage_root) as db:
                            db.execute("DELETE FROM outbox WHERE id=?", (row_id,))
                        completed += 1
                        continue
                    else:
                        if not self.enabled:
                            break
                        destination = self.general_topic_id if kind == "general" else self.operations_topic_id
                        await send_text(self.forum_id, int(destination), str(payload), event_key)
                except Exception as exc:
                    if not self.enabled:
                        break
                    retry_at = _retry_time(exc, attempts)
                    exhausted = attempts + 1 >= _MAX_RETRIES
                    reason = 'FloodWait' if getattr(exc, 'seconds', None) is not None else 'delivery failure'
                    disposition = 'abandoned after bounded retries' if exhausted else 'deferred for retry'
                    with _LOCK, _db(self._storage_root) as db:
                        if exhausted:
                            db.execute("DELETE FROM outbox WHERE id=?", (row_id,))
                            logger.error("Dropping Telegram forum audit row after bounded retries (%s)", kind)
                        else:
                            db.execute("UPDATE outbox SET attempts=attempts+1,next_attempt=? WHERE id=?",
                                       (retry_at, row_id))
                    # Delivery failures go to Operations, except Operations itself to avoid recursion.
                    if kind != "operations":
                        _insert(self._storage_root, key=f"delivery-error:{event_key}:{attempts + 1}",
                                kind="operations",
                                payload=f"Telegram forum audit {reason} ({kind}); {disposition}.",
                                topic_id=self.operations_topic_id)
                    logger.warning("Telegram forum audit %s (%s); %s", reason, kind, disposition)
                    if getattr(exc, "seconds", None) is not None:
                        self.defer_flood(retry_at)
                        break
                else:
                    if not self.enabled:
                        break  # Revoked/deleted profiles must not recreate their storage.
                    with _LOCK, _db(self._storage_root) as db:
                        db.execute("DELETE FROM outbox WHERE id=?", (row_id,))
                    completed += 1
            return completed



def _retry_time(exc: Exception, attempts: int) -> float:
    # FloodWait belongs in the durable schedule; no inbound task sleeps.
    wait = getattr(exc, "seconds", None)
    try:
        delay = max(1.0, float(wait)) if wait is not None else min(3600.0, 2.0 ** min(attempts, 11))
    except (TypeError, ValueError):
        delay = min(3600.0, 2.0 ** min(attempts, 11))
    return time.time() + delay


class _AsyncLock:
    """Lazy asyncio lock wrapper; keeps module import independent of an event loop."""
    def __init__(self):
        import asyncio
        self._lock = asyncio.Lock()
    async def __aenter__(self):
        await self._lock.acquire()
        return self
    async def __aexit__(self, *exc):
        self._lock.release()


def asyncio_lock():
    return _AsyncLock()


def _drain_lock_for(root: str | Path):
    # Coordination is process-local. Concurrent gateway processes for one profile
    # are unsupported; SQLite durability does not make Telegram delivery atomic.
    key = str(Path(root).expanduser().resolve(strict=False))
    with _LOCK:
        lock = _DRAIN_LOCKS.get(key)
        if lock is None:
            lock = asyncio_lock()
            _DRAIN_LOCKS[key] = lock
        return lock


def _refresh_service(profile: str) -> ForumAuditLogger | None:
    """Refresh the legacy profile lookup, preferring enabled primary then latest."""
    live = []
    retained = []
    for sequence, reference in _SERVICE_CANDIDATES.get(profile, []):
        service = reference()
        if service is not None:
            live.append((sequence, service))
            retained.append((sequence, reference))
    if retained:
        _SERVICE_CANDIDATES[profile] = retained
    else:
        _SERVICE_CANDIDATES.pop(profile, None)
    enabled = [(sequence, service) for sequence, service in live if service.enabled]
    candidates = enabled or live
    if not candidates:
        _SERVICES.pop(profile, None)
        return None
    _, selected = max(candidates,
                      key=lambda item: (item[1].enabled and item[1].transport_id is None,
                                        item[0]))
    _SERVICES[profile] = selected
    return selected


def _services_for_profile(profile: str) -> list[ForumAuditLogger]:
    with _LOCK:
        _refresh_service(profile)
        return [service for _, reference in _SERVICE_CANDIDATES.get(profile, [])
                if (service := reference()) is not None and service.enabled]


def status_for_profile(root: str | Path) -> dict[str, Any]:
    """Safe in-process status snapshot for the already owner-gated /logs view."""
    profile = str(_profile_root(root))
    services = _services_for_profile(profile)
    with _LOCK:
        service = _refresh_service(profile)
    if service is None:
        return {"configured": False, "enabled": False,
                "message": "No Telegram forum audit service is registered for this profile."}
    if not services:
        return service.status()
    seen: set[str] = set()
    pending = 0
    mapped_user_ids: set[int] = set()
    for candidate in services:
        key = str(candidate._storage_root.resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        status = candidate.status()
        pending += int(status.get("pending", 0))
        try:
            with _db(candidate._storage_root) as db:
                mapped_user_ids.update(row[0] for row in db.execute("SELECT user_id FROM user_topics"))
        except (OSError, sqlite3.Error):
            pass
    return {"configured": True, "enabled": True, "forum_id": service.forum_id,
            "pending": pending, "mapped_users": len(mapped_user_ids)}


def enqueue_operation(root: str | Path, text: str) -> bool:
    """Sync, durable enqueue-only hook for agent/tenant_policy.py.

    ``root`` is the profile HERMES_HOME. This function does no Telegram I/O; when
    no configured service has registered for the profile, it warns locally and
    returns False so policy denial never blocks or leaks to a user.
    """
    profile = str(_profile_root(root))
    with _LOCK:
        service = _refresh_service(profile)
    if service is None or not service.enabled:
        logger.warning("Telegram Operations notification not queued: forum audit is not configured for profile %s",
                       _profile_root(root).name)
        return False
    stamp = uuid.uuid4().hex
    return _insert(service._storage_root, key="operations:" + stamp, kind="operations",
                   payload=_bounded_text(text), topic_id=service.operations_topic_id)


__all__ = ["ForumAuditLogger", "enqueue_operation", "status_for_profile"]
