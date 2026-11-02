import sqlite3

import pytest

from agent.local_tenants import LocalTenantStore, _SQLiteCollection
from agent.mongo_tenants import Principal, TenantStoreError


def make_store(tmp_path, owner="owner-id", allowed=("alice",)):
    return LocalTenantStore(tmp_path / "profile", owner, allowed_user_ids=allowed)


def test_constructor_is_local_and_durable_without_mongo(tmp_path, monkeypatch):
    from agent import mongo_credentials
    monkeypatch.setattr(mongo_credentials, "_settings", lambda *_: pytest.fail("Mongo settings accessed"))
    monkeypatch.setattr(mongo_credentials, "_collection", lambda *_: pytest.fail("Mongo connected"))
    store = make_store(tmp_path)
    store.put(Principal("alice"), "memory", "note", {"text": "local"})
    assert store.database_path.name == "tenant-state.db"
    assert store.database_path.stat().st_mode & 0o777 == 0o600
    reopened = LocalTenantStore(tmp_path / "profile", "owner-id")
    assert reopened.get(Principal("alice"), "memory", "note") == {"text": "local"}
    assert sqlite3.connect(reopened.database_path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_user_data_and_controls_are_isolated(tmp_path):
    store = make_store(tmp_path)
    alice, bob, owner = Principal("alice"), Principal("bob"), Principal("owner-id")
    with pytest.raises(TenantStoreError):
        store.get(bob, "memory", "x")
    store.allow_user(owner, "bob", True)
    store.put(alice, "settings", "x", {"v": 1})
    assert store.get(bob, "settings", "x") is None
    assert store.get(alice, "settings", "x") == {"v": 1}
    with pytest.raises(TenantStoreError):
        store.get(alice, "credits", "x")
    with pytest.raises(TenantStoreError):
        store.put(alice, "providers", "x", {})
    with pytest.raises(TenantStoreError):
        store.allow_user(alice, "bob", False)
    with pytest.raises(TenantStoreError):
        store.ban_user(owner, "owner-id", True)


def test_revisions_usage_ledger_and_owner_enumeration(tmp_path):
    store = make_store(tmp_path)
    alice, owner = Principal("alice"), Principal("owner-id")
    assert store.put(alice, "memory", "x", 1) == 1
    assert store.put(alice, "memory", "x", 1, expected_revision=1) == 1
    with pytest.raises(TenantStoreError, match="revision conflict"):
        store.put(alice, "memory", "x", 2, expected_revision=0)
    assert store.record_usage(alice, "evt", 3, 0.25) is True
    assert store.record_usage(alice, "evt", 3, 0.25) is False
    with pytest.raises(TenantStoreError, match="identity conflict"):
        store.record_usage(alice, "evt", 4, 0.25)
    tenant = store.principal_tenant(alice)
    assert store.enumerate_tenant_data(owner, tenant)[0]["payload"] == 1


def test_owner_pinning_and_explicit_allowlist(tmp_path):
    home = tmp_path / "profile"
    store = LocalTenantStore(home, "owner-id", allowed_user_ids=("trusted-id",))
    store.ensure_owner()
    with pytest.raises(TenantStoreError, match="owner identity"):
        LocalTenantStore(home, "different-owner")
    assert store.get_access("trusted-id") == {"allowed": True, "banned": False}
    store.allow_user(Principal("owner-id"), "trusted-id", False)
    reopened = LocalTenantStore(home, "owner-id", allowed_user_ids=("trusted-id",))
    assert reopened.get_access("trusted-id") == {"allowed": False, "banned": False}


def test_unknown_query_and_update_operators_fail_closed(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(TenantStoreError):
        store._data.find_one({"$where": "true"})
    with pytest.raises(TenantStoreError):
        store._data.update_one({"_id": "x"}, {"$unset": {"payload": ""}})


def test_sqlite_accounting_collection_adapter_and_persistence(tmp_path):
    store = make_store(tmp_path)
    events = store.database["hermes_accounting_events"]
    events.insert_one({"_id": "event", "v": 1})
    assert events.find_one({"_id": "event"}, {"_id": 1}) == {"_id": "event"}
    sessions = store.database["hermes_accounting_sessions"]
    sessions.replace_one({"_id": "session"}, {"_id": "session", "revision": 1}, upsert=True)
    assert sessions.find_one({"_id": "session"})["revision"] == 1
    reopened = make_store(tmp_path, allowed=())
    assert reopened.database["hermes_accounting_events"].find_one({"_id": "event"})["v"] == 1


def test_sqlite_cas_two_connections(tmp_path):
    store = make_store(tmp_path)
    col = store.database["accounting_sessions"]
    col.insert_one({"_id": "s", "revision": 0})
    a = store.database["accounting_sessions"]
    b = store.database["accounting_sessions"]
    assert a.replace_one({"_id": "s", "revision": 0}, {"_id": "s", "revision": 1}).matched_count == 1
    assert b.replace_one({"_id": "s", "revision": 0}, {"_id": "s", "revision": 1}).matched_count == 0


def test_update_identity_mutation_rejected(tmp_path):
    store = make_store(tmp_path)
    for operator in ("$set", "$inc"):
        with pytest.raises(TenantStoreError):
            store._data.update_one({"_id": "x"}, {operator: {"_id": "other"}})


def test_exact_id_read_uses_keyed_query(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    col = store.database["proof"]
    col.insert_one({"_id": "target", "v": 1})
    original = col._connect
    class Proxy:
        def __init__(self, db): self.db = db
        def execute(self, sql, params=()):
            if "FROM tenant_documents" in sql and "AND id=?" not in sql:
                raise AssertionError("exact _id lookup scanned collection")
            return self.db.execute(sql, params)
        def close(self): self.db.close()
    monkeypatch.setattr(col, "_connect", lambda: Proxy(original()))
    assert col.find_one({"_id": "target"})["v"] == 1


def test_symlink_profile_and_database_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(TenantStoreError): LocalTenantStore(link, "owner")
    home = tmp_path / "db-link"
    home.mkdir()
    (home / "tenant-state.db").symlink_to(tmp_path / "outside.db")
    with pytest.raises(TenantStoreError): LocalTenantStore(home, "owner")


def test_sqlite_connections_are_closed(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    col = store.database["test"]
    connections = []
    connect = sqlite3.connect
    class Tracked:
        def __init__(self, db): self.db, self.closed = db, False; connections.append(self)
        def __getattr__(self, name): return getattr(self.db, name)
        def close(self): self.closed = True; self.db.close()
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: Tracked(connect(*a, **kw)))
    col.find_one({"_id": "missing"})
    assert connections and all(c.closed for c in connections)


def test_mongo_tenant_api_readback_after_reopen(tmp_path):
    store = make_store(tmp_path)
    owner, alice = Principal("owner-id"), Principal("alice")
    store.put(alice, "settings", "theme", {"mode": "dark"})
    assert store.record_usage(alice, "durable-event", 2, 0.5)
    store.ban_user(owner, "alice", True)
    store.allow_user(owner, "bob", True)
    reopened = LocalTenantStore(tmp_path / "profile", "owner-id")
    assert reopened.get_access("alice") == {"allowed": True, "banned": True}
    assert reopened.get_access("bob") == {"allowed": True, "banned": False}
    assert reopened._usage.find_one({"event_id": "durable-event"})["units"] == 2
    with pytest.raises(TenantStoreError):
        reopened.get(alice, "settings", "theme")
    reopened.ban_user(owner, "alice", False)
    assert reopened.get(Principal("alice"), "settings", "theme") == {"mode": "dark"}


def test_constructor_pins_owner_before_direct_data_writes(tmp_path):
    store = LocalTenantStore(tmp_path, "100")
    store.put(Principal("100"), "profile", "private", {"owner": "100"})
    with pytest.raises(TenantStoreError, match="owner"):
        LocalTenantStore(tmp_path, "200")
    assert LocalTenantStore(tmp_path, "100").get(Principal("100"), "profile", "private") == {"owner": "100"}
