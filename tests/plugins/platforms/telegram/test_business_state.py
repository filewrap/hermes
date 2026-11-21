"""Tests for the Telegram Business Mode state store.

Business connections (with rights) + the ``business_messages`` recent
table: record/list, 7-day prune, 500-row cap. Drafts are gone in v2 —
direct send replaced draft-and-approve, so the drafts table is dropped
on open and no draft API remains.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from plugins.platforms.telegram.business_state import BusinessStateDB


@pytest.fixture()
def db(tmp_path):
    """Fresh business state DB."""
    sdb = BusinessStateDB(tmp_path / "biz_state.db")
    yield sdb
    sdb.close()


class TestStateDbIsOwnerOnly:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes only")
    def test_state_db_is_readable_by_its_owner_only(self, tmp_path):
        # The store holds the customer conversation, so neither the file
        # nor its directory may be readable by another local account.
        sdb = BusinessStateDB(tmp_path / "telegram-business" / "state.db")
        try:
            path = tmp_path / "telegram-business" / "state.db"
            assert path.stat().st_mode & 0o077 == 0, oct(path.stat().st_mode)
            assert path.parent.stat().st_mode & 0o077 == 0, oct(path.parent.stat().st_mode)
        finally:
            sdb.close()


class TestBusinessConnectionPersistence:
    def test_upsert_and_get(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="42", can_reply=True, is_enabled=True,
        )
        row = db.get_telegram_business_connection("c1")
        assert row is not None
        assert row["connection_id"] == "c1"
        assert row["owner_user_id"] == "42"
        assert row["owner_chat_id"] == "42"
        assert row["can_reply"] is True
        assert row["is_enabled"] is True
        assert row["auto_draft"] is True
        assert row["paused_chats"] == []

    def test_upsert_preserves_auto_draft_and_paused(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="42", can_reply=True, is_enabled=True,
        )
        db.set_telegram_business_auto_draft("c1", auto_draft=False)
        db.set_telegram_business_paused_chats("c1", ["200", "300"])
        # Simulate Telegram re-sending the BusinessConnection
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="42", can_reply=False, is_enabled=True,
        )
        row = db.get_telegram_business_connection("c1")
        # can_reply updated, but owner preferences kept
        assert row["can_reply"] is False
        assert row["auto_draft"] is False
        assert sorted(row["paused_chats"]) == ["200", "300"]

    def test_list_enabled_filter(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="42", can_reply=True, is_enabled=True,
        )
        db.upsert_telegram_business_connection(
            connection_id="c2", owner_user_id="42",
            owner_chat_id="42", can_reply=False, is_enabled=False,
        )
        active = db.list_telegram_business_connections(
            owner_user_id="42", enabled_only=True,
        )
        all_rows = db.list_telegram_business_connections(
            owner_user_id="42", enabled_only=False,
        )
        assert len(active) == 1 and active[0]["connection_id"] == "c1"
        assert len(all_rows) == 2

    def test_get_returns_none_for_unknown(self, db):
        assert db.get_telegram_business_connection("missing") is None

    def test_rights_round_trip(self, db):
        rights = {"reply": True, "read_messages": True, "manage_stories": False}
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="42", can_reply=True, is_enabled=True,
            rights=rights,
        )
        row = db.get_telegram_business_connection("c1")
        assert row["rights"] == rights

    def test_rights_default_to_empty_when_not_provided(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="42", can_reply=True, is_enabled=True,
        )
        assert db.get_telegram_business_connection("c1")["rights"] == {}

    def test_upsert_without_rights_preserves_stored_rights(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="42", can_reply=True, is_enabled=True,
            rights={"reply": True},
        )
        # A later upsert that carries no rights (e.g. marking the
        # connection disabled) must not wipe the stored permissions.
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="42", can_reply=True, is_enabled=False,
        )
        row = db.get_telegram_business_connection("c1")
        assert row["rights"] == {"reply": True}
        assert row["is_enabled"] is False

    def test_delete_disabled_connections_for_owner(self, db):
        for conn_id, owner, enabled in (
            ("c1", "42", True), ("c2", "42", False), ("c3", "42", False),
            ("c4", "77", False),
        ):
            db.upsert_telegram_business_connection(
                connection_id=conn_id, owner_user_id=owner,
                owner_chat_id=owner, can_reply=True, is_enabled=enabled,
            )
        removed = db.delete_disabled_connections_for_owner("42")
        assert removed == 2
        remaining = db.list_telegram_business_connections(
            owner_user_id="42", enabled_only=False)
        assert [r["connection_id"] for r in remaining] == ["c1"]
        # Another owner's dead connection is untouched.
        assert db.get_telegram_business_connection("c4") is not None
        assert db.delete_disabled_connections_for_owner("42") == 0

    def test_opening_a_v1_store_drops_the_drafts_table(self, tmp_path):
        # A v1 store: connections without rights_json, a drafts table with
        # a pending row. Opening it with the v2 store migrates it: the
        # rights column appears, the drafts table (and its rows) go, and
        # connections + messages survive.
        path = tmp_path / "v1_state.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE business_connections (
                connection_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,
                owner_chat_id TEXT NOT NULL, can_reply INTEGER NOT NULL DEFAULT 0,
                is_enabled INTEGER NOT NULL DEFAULT 1, auto_draft INTEGER NOT NULL DEFAULT 1,
                paused_chats TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
                updated_at REAL NOT NULL);
            CREATE TABLE business_drafts (
                draft_id INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT NOT NULL,
                owner_chat_id TEXT NOT NULL, customer_chat_id TEXT NOT NULL,
                customer_msg_id TEXT, customer_text TEXT NOT NULL,
                draft_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                owner_message_id TEXT, final_sent_text TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE business_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT NOT NULL,
                customer_chat_id TEXT NOT NULL, customer_msg_id TEXT,
                customer_name TEXT, text TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT 'in', created_at REAL NOT NULL);
            INSERT INTO business_connections VALUES
                ('c1', '42', '42', 1, 1, 1, '[]', 1000.0, 1000.0);
            INSERT INTO business_drafts
                (connection_id, owner_chat_id, customer_chat_id, customer_text,
                 draft_text, created_at, updated_at, expires_at)
                VALUES ('c1', '42', '200', 'hi', 'hello', 1000.0, 1000.0, 90000.0);
            INSERT INTO business_messages
                (connection_id, customer_chat_id, text, direction, created_at)
                VALUES ('c1', '200', 'hi', 'in', 1000.0);
        """)
        conn.commit()
        conn.close()

        sdb = BusinessStateDB(path)
        try:
            tables = {r[0] for r in sdb._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert "business_drafts" not in tables
            assert not hasattr(sdb, "create_telegram_business_draft")
            row = sdb.get_telegram_business_connection("c1")
            assert row is not None and row["rights"] == {}
            assert [r["text"] for r in sdb.list_recent_business_messages("42")] == ["hi"]
        finally:
            sdb.close()


class TestBusinessRecentMessages:
    @pytest.fixture(autouse=True)
    def _conn(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="42", can_reply=True, is_enabled=True,
        )
        db.upsert_telegram_business_connection(
            connection_id="c2", owner_user_id="77",
            owner_chat_id="77", can_reply=True, is_enabled=True,
        )

    def test_record_and_list_newest_first(self, db):
        db.record_business_message(
            "c1", "200", "m1", "Carol", "first", "in",
        )
        db.record_business_message(
            "c1", "200", "m2", "Carol", "second", "in",
        )
        rows = db.list_recent_business_messages("42")
        assert [r["text"] for r in rows] == ["second", "first"]
        assert rows[0]["direction"] == "in"
        assert rows[0]["customer_name"] == "Carol"
        assert rows[0]["customer_chat_id"] == "200"
        assert rows[0]["connection_id"] == "c1"

    def test_list_recent_scoped_to_owner(self, db):
        db.record_business_message("c1", "200", "m1", "Carol", "for 42", "in")
        db.record_business_message("c2", "300", "m1", "Dave", "for 77", "in")
        assert [r["text"] for r in db.list_recent_business_messages("42")] == ["for 42"]
        assert [r["text"] for r in db.list_recent_business_messages("77")] == ["for 77"]
        assert db.list_recent_business_messages("nobody") == []

    def test_list_recent_limit(self, db):
        for i in range(5):
            db.record_business_message("c1", "200", f"m{i}", "Carol", f"msg {i}", "in")
        rows = db.list_recent_business_messages("42", limit=2)
        assert [r["text"] for r in rows] == ["msg 4", "msg 3"]
        assert len(db.list_recent_business_messages("42", limit=10)) == 5

    def test_record_prunes_older_than_seven_days(self, db):
        db.record_business_message("c1", "200", "m1", "Carol", "old", "in")
        with db._lock:
            db._conn.execute(
                "UPDATE business_messages SET created_at = ?",
                (time.time() - 8 * 86400.0,),
            )
            db._conn.commit()
        db.record_business_message("c1", "200", "m2", "Carol", "new", "in")
        assert [r["text"] for r in db.list_recent_business_messages("42")] == ["new"]

    def test_record_caps_at_500_rows(self, db):
        for i in range(505):
            db.record_business_message("c1", "200", f"m{i}", "Carol", f"msg {i}", "in")
        rows = db.list_recent_business_messages("42", limit=1000)
        assert len(rows) == 500
        texts = {r["text"] for r in rows}
        assert "msg 504" in texts
        assert "msg 4" not in texts
        assert "msg 5" in texts

    def test_record_out_direction(self, db):
        db.record_business_message("c1", "200", None, "", "sent reply", "out")
        rows = db.list_recent_business_messages("42")
        assert len(rows) == 1
        assert rows[0]["direction"] == "out"
        assert rows[0]["customer_msg_id"] is None
