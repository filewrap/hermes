"""Historical Mongo SQL archive backend tests, not production routing config."""
import hashlib
import sqlite3
from pathlib import Path

import mongomock
import pytest

from agent import mongo_sql_cache as bridge


def setup_home(tmp_path, monkeypatch):
    # Exercise the retired archive hook explicitly; URI/flags never enable it.
    monkeypatch.setattr("agent.storage_mode.mongo_primary_enabled", lambda home=None: True)
    root = tmp_path / "hermes"
    root.mkdir()
    (root / ".env").write_text("MULTIUSER_ENABLED=true\nOWNER_TELEGRAM_ID=7\n")
    db = mongomock.MongoClient().db
    monkeypatch.setattr(bridge, "_db_for", lambda home, database=None: database or db)
    monkeypatch.setattr(bridge, "get_process_hermes_home", lambda: root)
    return root, db


def make_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE durable (id INTEGER PRIMARY KEY, text TEXT, payload BLOB)")
    con.execute("INSERT INTO durable VALUES (1, 'secret plaintext', ?) ", (b'\x00abc',))
    con.execute("CREATE VIRTUAL TABLE search_fts USING fts5(text)")
    con.execute("INSERT INTO search_fts VALUES ('rebuildable')")
    con.commit(); con.close()


def test_bootstrap_is_readonly_and_encodes_bytes(tmp_path, monkeypatch):
    root, db = setup_home(tmp_path, monkeypatch)
    path = root / "state.db"; make_db(path)
    before = hashlib.sha256(path.read_bytes()).digest()
    receipt = bridge.bootstrap_sql_cache(root, path, db)
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert receipt["tables"] == 2
    row = db.hermes_sql_rows.find_one()
    assert row is not None
    assert db.hermes_sql_versions.count_documents({}) == 1
    assert len(list(db.hermes_sql_heads.find({}))) == 1
    assert next(iter(db.hermes_sql_heads.find({})))["revision"] == 1
    assert bridge.verify_sql_cache(root, path, db)["verified"]


def test_connection_hydrates_remote_authority_and_commits_before_return(tmp_path, monkeypatch):
    root, db = setup_home(tmp_path, monkeypatch)
    path = root / "state.db"; make_db(path)
    bridge.bootstrap_sql_cache(root, path, db)
    # Deliberately diverge local cache; Mongo remains authoritative.
    local = sqlite3.connect(path); local.execute("DELETE FROM durable"); local.commit(); local.close()
    con = bridge.connect(path, isolation_level=None)
    assert con.execute("SELECT text, payload FROM durable").fetchone() == ("secret plaintext", b"\x00abc")
    con.execute("UPDATE durable SET text='new value' WHERE id=1")
    assert next(iter(db.hermes_sql_heads.find({})))["revision"] == 2
    assert db.hermes_sql_rows.count_documents({"payload.text": "new value"}) == 1
    con.close()


def test_failed_cas_rolls_back_without_overwriting(tmp_path, monkeypatch):
    root, db = setup_home(tmp_path, monkeypatch)
    path = root / "state.db"; make_db(path)
    bridge.bootstrap_sql_cache(root, path, db)
    con = bridge.connect(path)
    con.execute("UPDATE durable SET text='local pending'")
    # Simulate competing committed Mongo writer.
    head = db.hermes_sql_heads.find_one({})
    db.hermes_sql_heads.update_one({"_id": head["_id"]}, {"$set": {"revision": 99}})
    with pytest.raises(bridge.SQLCacheConflict): con.commit()
    assert con.execute("SELECT text FROM durable").fetchone()[0] == "secret plaintext"
    assert db.hermes_sql_heads.find_one({"_id": head["_id"]})["revision"] == 99
    con.close()


def test_mongo_unavailable_fails_closed_and_database_outside_root_is_plain_sqlite(tmp_path, monkeypatch):
    root, _ = setup_home(tmp_path, monkeypatch)
    path = root / "state.db"; make_db(path)
    with pytest.raises(bridge.SQLCacheError, match="not bootstrapped"):
        bridge.connect(path)
    outside = tmp_path / "outside.db"
    con = bridge.connect(outside)
    assert type(con) is sqlite3.Connection
    con.close()


def test_owner_scope_policy_blocks_nonowner_before_root_access(tmp_path, monkeypatch):
    root, _ = setup_home(tmp_path, monkeypatch)
    class Scope:
        principal = type("Principal", (), {"user_id": "8"})()
    monkeypatch.setattr("agent.tenant_scope.current_tenant_scope", lambda: Scope())
    with pytest.raises(PermissionError): bridge.connect(root / "state.db")


def test_idempotent_bootstrap_and_ftsskip(tmp_path, monkeypatch):
    root, db = setup_home(tmp_path, monkeypatch)
    path = root / "state.db"; make_db(path)
    first = bridge.bootstrap_sql_cache(root, path, db)
    second = bridge.bootstrap_sql_cache(root, path, db)
    assert first["revision"] == second["revision"] == 1
    assert bridge.verify_sql_cache(root, path, db)["verified"]


def test_independent_database_namespaces_and_conflicting_bootstrap(tmp_path, monkeypatch):
    root, db = setup_home(tmp_path, monkeypatch)
    a, b = root / "state.db", root / "kanban.db"
    make_db(a); make_db(b)
    bridge.bootstrap_sql_cache(root, a, db); bridge.bootstrap_sql_cache(root, b, db)
    assert len(list(db.hermes_sql_heads.find({}))) == 2
    before = list(db.hermes_sql_heads.find({}))
    local = sqlite3.connect(a); local.execute("UPDATE durable SET text='other'"); local.commit(); local.close()
    with pytest.raises(bridge.SQLCacheConflict): bridge.bootstrap_sql_cache(root, a, db)
    assert list(db.hermes_sql_heads.find({})) == before


def test_duplicate_nonprimary_rows_and_cursor_context_commit(tmp_path, monkeypatch):
    root, db = setup_home(tmp_path, monkeypatch)
    path = root / "duplicates.db"
    raw = sqlite3.connect(path); raw.execute("CREATE TABLE dupe(value TEXT)"); raw.executemany("INSERT INTO dupe VALUES (?)", [("x",), ("x",)]); raw.commit(); raw.close()
    bridge.bootstrap_sql_cache(root, path, db)
    path.unlink()
    con = bridge.connect(path)
    assert con.execute("SELECT count(*) FROM dupe").fetchone()[0] == 2
    with con:
        cur = con.cursor(); cur.execute("INSERT INTO dupe VALUES ('y')")
    assert bridge.verify_sql_cache(root, path, db)["verified"]


def test_cursor_executemany_and_executescript_publish(tmp_path, monkeypatch):
    root, db = setup_home(tmp_path, monkeypatch)
    path=root / "cursor.db"; make_db(path); bridge.bootstrap_sql_cache(root,path,db)
    con=bridge.connect(path,isolation_level=None)
    c=con.cursor(); c.execute("UPDATE durable SET text='cursor'")
    assert next(iter(db.hermes_sql_heads.find({})))["revision"] == 2
    c.executemany("INSERT INTO durable(id,text,payload) VALUES(?,?,?)", [(2,"two",b"2")])
    con.executescript("CREATE TABLE extra(v TEXT); INSERT INTO extra VALUES('script');")
    assert bridge.verify_sql_cache(root,path,db)["verified"]
    con.close()


def test_corrupt_existing_blob_rejected_without_head_update(tmp_path, monkeypatch):
    root, db = setup_home(tmp_path, monkeypatch)
    path=root / "tamper.db"; make_db(path); bridge.bootstrap_sql_cache(root,path,db)
    head=db.hermes_sql_heads.find_one({}); old_revision=head["revision"]
    blob=db.hermes_sql_rows.find_one({}); db.hermes_sql_rows.update_one({"_id":blob["_id"]},{"$set":{"payload":{"tampered":True}}})
    with pytest.raises(bridge.SQLCacheError,match="integrity"):
        bridge.connect(path)
    assert db.hermes_sql_heads.find_one({"_id":head["_id"]})["revision"] == old_revision
