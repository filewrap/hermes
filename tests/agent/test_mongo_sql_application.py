"""Historical Mongo SQL archive integration tests; not a production config option."""
from pathlib import Path
import sqlite3

import mongomock
import pytest

from agent import mongo_credentials, mongo_sql_cache, mongo_accounting
from agent.mongo_tenants import MongoTenantStore, Principal
from agent.mongo_accounting import MongoAccountingStore
from hermes_state import SessionDB


@pytest.fixture
def application(tmp_path, monkeypatch):
    # Retain coverage of archive/migration semantics without URI-based routing.
    monkeypatch.setattr("agent.storage_mode.mongo_primary_enabled", lambda home=None: False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mongo_sql_cache, "get_process_hermes_home", lambda: tmp_path)
    env = tmp_path / ".env"
    env.write_text("OWNER_TELEGRAM_ID=1\nMULTIUSER_ENABLED=false\nMONGODB_URI=mongodb://unused/hermes\n")
    original = SessionDB(tmp_path / "state.db")
    original.create_session("owner", "telegram", user_id="1", chat_id="1")
    original.create_session("allowed", "telegram", user_id="2", chat_id="2")
    original.append_message("owner", "user", "owner initial message")
    original.close()
    database = mongomock.MongoClient().hermes
    monkeypatch.setattr(mongo_sql_cache, "_db_for", lambda home, database_override=None: database if database_override is None else database_override)
    monkeypatch.setattr(mongo_credentials, "_settings", lambda home: mongo_credentials._Settings("mongodb://unused/hermes"))
    tenants = MongoTenantStore(tmp_path, "1", database=database)
    tenants.ensure_owner()
    tenants.allow_user(Principal("1"), "2", True)
    accounting = MongoAccountingStore(tmp_path, database=database)
    monkeypatch.setattr(mongo_accounting, "account_store", lambda home: accounting)
    mongo_sql_cache.bootstrap_sql_cache(tmp_path, tmp_path / "state.db", mongo_database=database)
    monkeypatch.setattr("agent.storage_mode.mongo_primary_enabled", lambda home=None: True)
    env.write_text(env.read_text().replace("ENABLED=false", "ENABLED=true"))
    return tmp_path, database, tenants, accounting


def test_real_state_database_persists_mongo_and_attributes_owner_and_user(application):
    home, database, _tenants, accounting = application
    db = SessionDB(home / "state.db")
    try:
        db.append_message("allowed", "user", "user two private message")
        db.update_token_counts("owner", input_tokens=7, output_tokens=2, api_call_count=1)
        db.update_token_counts("allowed", input_tokens=11, output_tokens=3, api_call_count=1)
        assert accounting.totals(Principal("1"))["input_tokens"] == 7
        assert accounting.totals(Principal("2"))["input_tokens"] == 11
        assert db.get_session("allowed")["user_id"] == "2"
    finally:
        db.close()
    connection = mongo_sql_cache.connect(home / "state.db")
    try:
        connection.execute("CREATE TABLE delivery_obligations (obligation_id TEXT PRIMARY KEY, content TEXT)")
        connection.execute("INSERT INTO delivery_obligations VALUES ('pending-1', 'deliver after recovery')")
        connection.commit()
    finally:
        connection.close()
    receipt = mongo_sql_cache.verify_sql_cache(home, home / "state.db", mongo_database=database)
    assert receipt["verified"]
    assert database.hermes_sql_heads.count_documents({}) == 1
    namespace = mongo_sql_cache._namespace(home / "state.db", home)
    _, snapshot = mongo_sql_cache._read_head(database, namespace)
    assert "messages" not in snapshot["tables"]
    assert not any(name.startswith("messages_fts") for name in snapshot["tables"])
    delivery_rows = snapshot["tables"]["delivery_obligations"]["rows"].values()
    assert any(row["content"] == "deliver after recovery" for row in delivery_rows)


def test_state_database_can_rebuild_empty_cache_from_mongo(application):
    home, database, _tenants, _accounting = application
    path = home / "state.db"
    path.unlink()
    with sqlite3.connect(path):
        pass
    db = SessionDB(path)
    try:
        assert db.get_session("owner")["user_id"] == "1"
        # Mongo reconstructs non-chat state only; transcripts remain local/HF-only.
        assert db.get_messages("owner") == []
    finally:
        db.close()
    assert mongo_sql_cache.verify_sql_cache(home, path, mongo_database=database)["verified"]


def test_tenant_transcript_documents_are_filtered_but_other_tenant_state_is_kept(tmp_path):
    path = tmp_path / "tenant-state.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tenant_documents (collection TEXT, id TEXT, body TEXT, PRIMARY KEY (collection, id))")
        connection.executemany(
            "INSERT INTO tenant_documents VALUES (?, ?, ?)",
            [
                ("hermes_tenant_data", "history", '{"kind":"profile","key":"history:dm:1","payload":[{"role":"user","content":"private transcript"}]}'),
                ("hermes_tenant_data", "settings", '{"kind":"settings","key":"environment","payload":{"theme":"dark"}}'),
            ],
        )
        snapshot = mongo_sql_cache._snapshot(connection)
    rows = list(snapshot["tables"]["tenant_documents"]["rows"].values())
    assert len(rows) == 1
    assert rows[0]["id"] == "settings"
    assert "private transcript" not in repr(snapshot)


@pytest.mark.parametrize("cursor", [False, True])
def test_outer_savepoint_and_explicit_commit_are_durable(application, cursor):
    home, database, _, _ = application
    connection = mongo_sql_cache.connect(home / "state.db", isolation_level=None)
    handle = connection.cursor() if cursor else connection
    try:
        handle.execute("SAVEPOINT outer_write")
        handle.execute("CREATE TABLE extra_owned_records (id INTEGER PRIMARY KEY, body TEXT)")
        handle.execute("INSERT INTO extra_owned_records VALUES (1, 'durable')")
        handle.execute("RELEASE SAVEPOINT outer_write")
        assert not connection.in_transaction
        assert mongo_sql_cache.verify_sql_cache(home, home / "state.db", mongo_database=database)["verified"]
        handle.execute("BEGIN")
        handle.execute("INSERT INTO extra_owned_records VALUES (2, 'also durable')")
        handle.execute("COMMIT")
        assert mongo_sql_cache.verify_sql_cache(home, home / "state.db", mongo_database=database)["verified"]
    finally:
        connection.close()


def test_explicit_transaction_script_cannot_bypass_mongo(application):
    home, database, _, _ = application
    connection = mongo_sql_cache.connect(home / "state.db", isolation_level=None)
    try:
        connection.executescript("BEGIN; CREATE TABLE extra_script_data (body TEXT); INSERT INTO extra_script_data VALUES ('durable'); COMMIT;")
        assert mongo_sql_cache.verify_sql_cache(home, home / "state.db", mongo_database=database)["verified"]
    finally:
        connection.close()


@pytest.mark.parametrize("uid", ["1", "2"])
def test_isolated_turn_including_group_owner_cannot_read_owner_sql_or_uri(application, uid):
    from agent.tenant_scope import TenantScope, set_tenant_scope, reset_tenant_scope
    home, _database, _, _ = application
    connection = mongo_sql_cache.connect(home / "state.db")
    scope = TenantScope(home, Principal(uid), "group", home / ".tenant-cache" / uid, "owner" if uid == "1" else "user:" + uid)
    token = set_tenant_scope(scope)
    try:
        with pytest.raises(PermissionError):
            mongo_sql_cache.connect(home / "state.db")
        with pytest.raises(PermissionError):
            mongo_sql_cache.connect(f"file:{home / 'state.db'}?mode=ro", uri=True)
        with pytest.raises(PermissionError):
            connection.execute("SELECT * FROM sessions")
    finally:
        reset_tenant_scope(token)
        connection.close()


def test_direct_local_chat_edits_remain_local_and_do_not_change_mongo(application):
    home, database, _, _ = application
    connection = mongo_sql_cache.connect(home / "state.db")
    try:
        with sqlite3.connect(home / "state.db") as unsafe_cache_editor:
            unsafe_cache_editor.execute("UPDATE messages SET content='local-only-corruption'")
        assert connection.execute("SELECT content FROM messages").fetchone()[0] == "local-only-corruption"
        assert mongo_sql_cache.verify_sql_cache(home, home / "state.db", mongo_database=database)["verified"]
    finally:
        connection.close()
