"""Plugin-owned SQLite state for Telegram Business Mode.

Two tables, all living in the store's own database file
(``<profile-home>/telegram-business/state.db``) — the framework never
touches Hermes' core ``state.db`` schema:

  business_connections — one row per Telegram Business account that linked
    the bot via BotFather Business Mode. Updated by BotBusinessConnection
    updates (established / edited / ended). Carries the connection's
    rights (``BusinessBotRights`` as JSON) for the /biz status display.

  business_messages — recent customer messages (both directions) for the
    reply-turn context and ``/biz recent``. Pruned to 7 days / 500 rows.

Drafts are gone in v2: direct send replaced draft-and-approve, so the
``business_drafts`` table is dropped on open and no draft API remains.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS business_connections (
    connection_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    owner_chat_id TEXT NOT NULL,
    can_reply INTEGER NOT NULL DEFAULT 0,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    auto_draft INTEGER NOT NULL DEFAULT 1,
    paused_chats TEXT NOT NULL DEFAULT '[]',
    rights_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_biz_conn_owner
    ON business_connections(owner_user_id);

CREATE TABLE IF NOT EXISTS business_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    customer_chat_id TEXT NOT NULL,
    customer_msg_id TEXT,
    customer_name TEXT,
    text TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'in',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_biz_messages_conn_customer
    ON business_messages(connection_id, customer_chat_id, created_at);

CREATE INDEX IF NOT EXISTS idx_biz_messages_created
    ON business_messages(created_at);
"""

# Recent-message retention: older than this, or beyond this row count, is pruned.
RECENT_MAX_AGE_SECONDS = 7 * 86400.0
RECENT_MAX_ROWS = 500


def _restrict_to_owner(path: Path) -> None:
    """Make ``path`` readable and writable by its owner only (a no-op on Windows).

    The store holds the customer conversation and the replies sent to customers, so another
    local account must not read them. Best-effort: a filesystem that does not carry POSIX
    modes (FAT, a network share) keeps the default permissions rather than failing the plugin.
    """
    try:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    except OSError:
        pass


class BusinessStateDB:
    """Thread-safe SQLite store for connections + recent messages."""

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_to_owner(db_path.parent)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _restrict_to_owner(db_path)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """v2 migrations on existing stores, idempotent.

        Adds the ``rights_json`` column to connections created by v1,
        and drops the ``business_drafts`` table: direct send replaced
        draft-and-approve, so pending v1 drafts have no UI left to act
        on them.
        """
        cols = {row[1] for row in
                self._conn.execute("PRAGMA table_info(business_connections)")}
        if "rights_json" not in cols:
            self._conn.execute(
                "ALTER TABLE business_connections "
                "ADD COLUMN rights_json TEXT NOT NULL DEFAULT '{}'")
        self._conn.execute("DROP TABLE IF EXISTS business_drafts")
        self._conn.execute("DROP INDEX IF EXISTS idx_biz_drafts_conn_customer")

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                logger.debug("Business state DB close failed", exc_info=True)

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def upsert_telegram_business_connection(
        self,
        *,
        connection_id: str,
        owner_user_id: str,
        owner_chat_id: str,
        can_reply: bool,
        is_enabled: bool,
        rights: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert or update a connection row.

        Preserves ``auto_draft`` and ``paused_chats`` across updates so the
        owner's preferences survive Telegram re-issuing the connection.
        ``rights=None`` likewise preserves the stored permissions (a caller
        that only flips the enabled flag must not wipe them); pass an
        explicit dict — possibly empty — to overwrite them.
        """
        now = time.time()
        with self._lock:
            if rights is None:
                row = self._conn.execute(
                    "SELECT rights_json FROM business_connections "
                    "WHERE connection_id = ?",
                    (str(connection_id),),
                ).fetchone()
                rights_json = str(row["rights_json"]) if row is not None else "{}"
            else:
                rights_json = json.dumps(dict(rights))
            self._conn.execute(
                """
                INSERT INTO business_connections (
                    connection_id, owner_user_id, owner_chat_id,
                    can_reply, is_enabled, auto_draft, paused_chats,
                    rights_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, '[]', ?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                    owner_user_id = excluded.owner_user_id,
                    owner_chat_id = excluded.owner_chat_id,
                    can_reply = excluded.can_reply,
                    is_enabled = excluded.is_enabled,
                    rights_json = excluded.rights_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(connection_id), str(owner_user_id), str(owner_chat_id),
                    1 if can_reply else 0, 1 if is_enabled else 0,
                    rights_json, now, now,
                ),
            )
            self._conn.commit()

    @staticmethod
    def _conn_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["paused_chats"] = json.loads(d.get("paused_chats") or "[]")
        except (TypeError, ValueError):
            d["paused_chats"] = []
        try:
            rights = json.loads(d.pop("rights_json", None) or "{}")
        except (TypeError, ValueError):
            rights = {}
        d["rights"] = rights if isinstance(rights, dict) else {}
        d["can_reply"] = bool(d.get("can_reply"))
        d["is_enabled"] = bool(d.get("is_enabled"))
        d["auto_draft"] = bool(d.get("auto_draft", 1))
        return d

    def delete_disabled_connections_for_owner(self, owner_user_id: str) -> int:
        """Delete an owner's ended connections; only the active one stays.

        Returns how many rows were removed. Other owners are untouched.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM business_connections "
                "WHERE owner_user_id = ? AND is_enabled = 0",
                (str(owner_user_id),),
            )
            self._conn.commit()
            return int(cur.rowcount)

    def get_telegram_business_connection(
        self, connection_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM business_connections WHERE connection_id = ?",
                (str(connection_id),),
            ).fetchone()
        return self._conn_row_to_dict(row) if row is not None else None

    def list_telegram_business_connections(
        self, *, owner_user_id: Optional[str] = None, enabled_only: bool = True
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM business_connections"
        clauses: List[str] = []
        params: List[Any] = []
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(str(owner_user_id))
        if enabled_only:
            clauses.append("is_enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._conn_row_to_dict(r) for r in rows]

    def set_telegram_business_auto_draft(
        self, connection_id: str, *, auto_draft: bool
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE business_connections SET auto_draft = ?, updated_at = ? "
                "WHERE connection_id = ?",
                (1 if auto_draft else 0, time.time(), str(connection_id)),
            )
            self._conn.commit()

    def set_telegram_business_paused_chats(
        self, connection_id: str, paused_chats: List[str]
    ) -> None:
        payload = json.dumps([str(c) for c in (paused_chats or [])])
        with self._lock:
            self._conn.execute(
                "UPDATE business_connections SET paused_chats = ?, updated_at = ? "
                "WHERE connection_id = ?",
                (payload, time.time(), str(connection_id)),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Recent messages (reply-turn context + /biz recent)
    # ------------------------------------------------------------------

    def record_business_message(
        self,
        connection_id: str,
        customer_chat_id: str,
        customer_msg_id: Optional[str],
        customer_name: Optional[str],
        text: str,
        direction: str,
    ) -> int:
        """Append one message to the recent table and prune it.

        ``direction`` is ``in`` (customer wrote) or ``out`` (a reply was
        sent to the customer). Prunes rows older than 7 days and caps the
        table at 500 rows (newest kept). Returns the row id.
        """
        if direction not in {"in", "out"}:
            raise ValueError(f"invalid business message direction: {direction}")
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO business_messages (
                    connection_id, customer_chat_id, customer_msg_id,
                    customer_name, text, direction, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(connection_id), str(customer_chat_id),
                    str(customer_msg_id) if customer_msg_id is not None else None,
                    customer_name, text or "", direction, now,
                ),
            )
            self._conn.execute(
                "DELETE FROM business_messages WHERE created_at < ?",
                (now - RECENT_MAX_AGE_SECONDS,),
            )
            self._conn.execute(
                "DELETE FROM business_messages WHERE id NOT IN ("
                "SELECT id FROM business_messages "
                "ORDER BY created_at DESC, id DESC LIMIT ?)",
                (RECENT_MAX_ROWS,),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_recent_business_messages(
        self,
        owner_chat_id: str,
        limit: int = 10,
        *,
        customer_chat_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Newest-first recent messages for one owner's connections."""
        sql = (
            "SELECT m.* FROM business_messages m "
            "JOIN business_connections c ON m.connection_id = c.connection_id "
            "WHERE c.owner_chat_id = ?"
        )
        params: List[Any] = [str(owner_chat_id)]
        if customer_chat_id is not None:
            sql += " AND m.customer_chat_id = ?"
            params.append(str(customer_chat_id))
        sql += " ORDER BY m.created_at DESC, m.id DESC LIMIT ?"
        params.append(max(0, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
