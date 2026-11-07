from pathlib import Path
import hashlib
import json
import sqlite3

import mongomock
import pytest

from agent import mongo_credentials
from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError
from agent.mongo_accounting import MongoAccountingStore
from agent.mongo_migration import build_plan, execute_plan


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=1\nMULTIUSER_ENABLED=false\nMONGODB_URI=mongodb://unused/hermes\nAPI_KEY=plaintext-test-key\nHERMES_MAX_ITERATIONS=7\n")
    (tmp_path / "config.yaml").write_text("model:\n  default: example-model\n")
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories/MEMORY.md").write_text("owner global memory")
    user = tmp_path / "personal/telegram/users/2"
    (user / "memory").mkdir(parents=True)
    (user / "settings.json").write_text(json.dumps({"name": "User Two"}))
    (user / "memory/MEMORY.md").write_text("user two private memory")
    dbpath = tmp_path / "state.db"
    with sqlite3.connect(dbpath) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,user_id TEXT,chat_id TEXT,input_tokens INTEGER, output_tokens INTEGER)")
        conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?)", [("a", "telegram", "1", "1", 10, 2), ("b", "telegram", "2", "2", 20, 4)])
    monkeypatch.setattr(mongo_credentials, "_settings", lambda home: mongo_credentials._Settings("mongodb://unused/hermes"))
    database = mongomock.MongoClient().hermes
    store = MongoTenantStore(tmp_path, "1", database=database)
    accounting = MongoAccountingStore(tmp_path, database=database)
    return tmp_path, store, accounting


def test_complete_inventory_plaintext_and_no_owner_copy(deployment):
    home, store, accounting = deployment
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    plan = build_plan(home, "1", ["1", "2"])
    calls = []
    def credentials(root, preserve_local):
        calls.append((root, preserve_local))
        return {"credential_records": 1}
    result = execute_plan(store, Principal("1"), plan, credential_sync=credentials, accounting=accounting)
    assert result["status"] == "complete"
    assert result["usage_verified"] == 2
    assert result["local_deleted"] == 0
    assert calls == [(home, True)]
    assert {p: p.read_bytes() for p in before} == before
    assert store.get(Principal("2"), "memory", "MEMORY.md") == "user two private memory"
    assert store.get(Principal("2"), "memory", "root:MEMORY.md") is None
    assert store.get(Principal("1"), "memory", "root:MEMORY.md") == "owner global memory"
    assert store.get(Principal("1"), "settings", "environment") == {"HERMES_MAX_ITERATIONS": "7"}
    assert accounting.totals(Principal("1"))["input_tokens"] == 10
    assert accounting.totals(Principal("2"))["input_tokens"] == 20
    profile = store._profiles.find_one({"user_id": "2"})
    assert profile["credits"] == 0 and profile["weight"] == 1
    with pytest.raises(TenantStoreError):
        store.get(Principal("2"), "providers", "env:API_KEY")


def test_verified_migration_marker_selects_mongo_primary_without_exposing_uri(deployment, monkeypatch):
    from agent.storage_mode import activate_mongo_primary, mongo_primary_enabled
    from gateway import mongo_multiuser

    home, store, accounting = deployment
    plan = build_plan(home, "1", ["1", "2"])
    receipt = execute_plan(
        store, Principal("1"), plan,
        credential_sync=lambda *_args, **_kwargs: {"credential_records": 1},
        accounting=accounting,
    )
    monkeypatch.setattr(
        mongo_credentials, "_bootstrap",
        lambda _home: {mongo_credentials.URI_ENV: "mongodb://unused/hermes"},
    )
    monkeypatch.setattr(
        mongo_credentials, "_collection", lambda _settings: store.database.deep_credentials,
    )
    assert not mongo_primary_enabled(home)
    marker_path = home / ".mongo-primary.json"
    forged = dict(receipt)
    forged["digest"] = "f" * 64
    with pytest.raises(ValueError, match="Persisted Mongo migration receipt"):
        activate_mongo_primary(home, forged, mongo_database=store.database)
    assert not marker_path.exists()
    marker = activate_mongo_primary(home, receipt, mongo_database=store.database)
    assert mongo_primary_enabled(home)
    assert "mongodb://" not in marker_path.read_text()
    assert marker["migration_digest"] == plan["digest"]
    assert isinstance(mongo_multiuser.store_for_home(home), MongoTenantStore)

    altered = dict(marker)
    altered["owner_id"] = "99"
    marker_path.write_text(json.dumps(altered))
    with pytest.raises(RuntimeError, match="does not match"):
        mongo_primary_enabled(home)


def test_explicit_migration_copies_credentials_before_primary_activation(deployment, monkeypatch):
    from agent.storage_mode import mongo_primary_enabled

    home, store, accounting = deployment
    monkeypatch.setattr(
        mongo_credentials, "_collection", lambda _settings: store.database.deep_credentials,
    )
    plan = build_plan(home, "1", ["1", "2"])
    receipt = execute_plan(store, Principal("1"), plan, accounting=accounting)
    assert receipt["credentials_verified"] >= 1
    assert store.database.deep_credentials.find_one({"name": "env"})["payload"]["API_KEY"] == "plaintext-test-key"
    assert not mongo_primary_enabled(home)


def test_safe_rerun_does_not_duplicate_usage_or_reset_access(deployment):
    home, store, accounting = deployment
    plan = build_plan(home, "1", ["1", "2"])
    sync = lambda *a, **k: {"credential_records": 1}
    execute_plan(store, Principal("1"), plan, credential_sync=sync, accounting=accounting)
    counts = (store._data.count_documents({}), accounting.events.count_documents({}))
    store.allow_user(Principal("1"), "2", False)
    execute_plan(store, Principal("1"), plan, credential_sync=sync, accounting=accounting)
    assert counts == (store._data.count_documents({}), accounting.events.count_documents({}))
    assert store.get_access("2")["allowed"] is False
    assert accounting.totals(Principal("1"))["input_tokens"] == 10


def test_conflicts_and_changed_sources_fail_before_writes(deployment):
    home, store, accounting = deployment
    plan = build_plan(home, "1", ["2"])
    store.ensure_owner()
    store.owner_put_tenant(Principal("1"), "owner", "settings", "environment", {"wrong": "value"})
    before = list(store._data.find())
    with pytest.raises(TenantStoreError, match="conflicting"):
        execute_plan(store, Principal("1"), plan, credential_sync=lambda *a, **k: pytest.fail("no credential write"), accounting=accounting)
    assert list(store._data.find()) == before
    assert store.database.hermes_migrations.count_documents({}) == 0
    (home / "config.yaml").write_text("changed: true\n")
    with pytest.raises(TenantStoreError, match="source changed"):
        execute_plan(store, Principal("1"), plan, accounting=accounting)


def test_ambiguous_telegram_owner_never_guessed(deployment):
    home, store, _accounting = deployment
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("INSERT INTO sessions VALUES ('unknown','telegram',NULL,'-100',0,0)")
    with pytest.raises(TenantStoreError, match="ambiguous"):
        build_plan(home, "1", ["1"])
    assert store._data.count_documents({}) == 0

# These are the nine preserved production identities, never their payloads. All
# documents below are synthetic and live only in this test's mongomock client.
_PROTECTED_IDS = frozenset({
    "04afce46511ce1e3e71cd6a5467ebab48d7e8e379ddb1f0cc15068a98bf52680",
    "10f15084a647fd75a6325ed1d2722d6e35b9e65bc383af1ae288f3c1ffbbaded",
    "1d68d22fb669c4d3d9448a40505a852d3d38813693af61092e6408b20e0683b1",
    "25c7040984dcdd77ea3090c83b40e6cbf5072fe8206d017a0c9b7e95eff2bd35",
    "36c044181404ebad9b8ffbeb400c9de98e3774046f64bada2615852ab9ee7b88",
    "41b683c3ceb5d733e481328b5a1def5e1d8d4428ac84570f8b32430a94fca97e",
    "5e70895a9a9a84113266e4eb7a7f4fe44b22a7a5704dc857be3eaa0af4a7da72",
    "637fc42bca053aaf4c110dbaf76b4b604131ab8aa8ae6e1f6a35a1f3e985e2d3",
    "b5e833eac41e529f50b3a71644393a7758108d890066897a00129fb8a3f48a19",
})


def test_verified_import_preserves_nine_unknown_credentials_without_payload_reads(deployment, monkeypatch):
    home, store, accounting = deployment
    (home / "auth.json").write_text(json.dumps({"synthetic": "owned-local-auth"}))
    plan = build_plan(home, "1", ["1", "2"])
    collection = store.database.deep_credentials
    unknown = [{"_id": ident, "name": "auth", "schema_version": 2,
                "storage": "plaintext", "revision": 1,
                "payload": {"synthetic": "not-a-migration-source"}} for ident in sorted(_PROTECTED_IDS)]
    collection.insert_many(unknown)
    monkeypatch.setattr(mongo_credentials, "_collection", lambda settings: collection)
    # Exercise the archived explicit migration primitive in this isolated test;
    # a saved URI no longer enables Mongo authority in ordinary serving paths.
    monkeypatch.setattr(mongo_credentials, "enabled", lambda home: True)
    allowed = {mongo_credentials._record_id(home, name) for name in ("env", "auth")}
    assert not allowed & _PROTECTED_IDS
    reads, writes = [], []
    # Fail on broad reads as well as exact reads of any protected ID. Mutation
    # operations are allowlisted too, proving no delete/update/overwrite occurs.
    with monkeypatch.context() as guard:
        for method in ("find_one", "find"):
            original = getattr(collection, method)
            def read(query, *args, _original=original, **kwargs):
                assert set(query) == {"_id"} and query["_id"] in allowed
                reads.append(query["_id"])
                return _original(query, *args, **kwargs)
            guard.setattr(collection, method, read)
        for method in ("insert_one", "replace_one", "update_one", "delete_one", "delete_many", "update_many"):
            original = getattr(collection, method)
            def write(document, *args, _original=original, _method=method, **kwargs):
                assert _method in {"insert_one", "replace_one"}
                assert document["_id"] in allowed
                writes.append(document["_id"])
                return _original(document, *args, **kwargs)
            guard.setattr(collection, method, write)
        for method in ("drop", "bulk_write", "insert_many", "aggregate", "find_one_and_update", "find_one_and_replace", "find_one_and_delete"):
            guard.setattr(collection, method, lambda *a, **k: pytest.fail("unscoped credential operation"))
        receipt = execute_plan(store, Principal("1"), plan, accounting=accounting)
        assert receipt["credentials_verified"] == 2
        execute_plan(store, Principal("1"), plan, accounting=accounting)
    assert reads and writes
    assert set(reads) <= allowed and set(writes) <= allowed
    assert list(collection.find({"_id": {"$in": sorted(_PROTECTED_IDS)}}).sort("_id")) == unknown
    assert collection.count_documents({}) == 11
    assert mongo_credentials.get_record(home, "auth") == {"synthetic": "owned-local-auth"}


def test_unrelated_tenant_profile_and_sql_head_are_not_read_or_changed(deployment, monkeypatch):
    home, store, accounting = deployment
    plan = build_plan(home, "1", ["1", "2"])
    unrelated = [
        (store._data, {"_id": "unrelated-owner-key", "tenant_id": "owner", "kind": "memory", "key": "unknown", "payload": "private"}),
        (store._data, {"_id": "unrelated-tenant", "tenant_id": "tg-99", "payload": "private"}),
        (store._profiles, {"_id": "tg-99", "user_id": "99", "profile_id": "tg-99", "allowed": False}),
        (store.database.hermes_sql_heads, {"_id": "state:unrelated", "version": "unknown", "revision": 1}),
    ]
    for collection, document in unrelated:
        collection.insert_one(document)
    with monkeypatch.context() as guard:
        for collection in (store._data, store._profiles, store.database.hermes_sql_heads):
            protected = {doc["_id"] for coll, doc in unrelated if coll is collection}
            for method in ("find_one", "find", "update_one", "replace_one", "delete_one", "delete_many", "update_many"):
                original = getattr(collection, method)
                def scoped(query, *args, _original=original, _protected=protected, **kwargs):
                    assert isinstance(query.get("_id"), str) and query["_id"] not in _protected
                    return _original(query, *args, **kwargs)
                guard.setattr(collection, method, scoped)
        execute_plan(store, Principal("1"), plan, credential_sync=lambda *a, **k: {"credential_records": 1}, accounting=accounting)
    for collection, document in unrelated:
        assert collection.find_one({"_id": document["_id"]}) == document


def test_colliding_profile_identity_fails_before_migration_writes(deployment):
    home, store, accounting = deployment
    plan = build_plan(home, "1", ["2"])
    store._profiles.insert_one({"_id": "tg-2", "user_id": "99", "profile_id": "tg-2"})
    with pytest.raises(TenantStoreError, match="Conflicting user profile"):
        execute_plan(store, Principal("1"), plan, credential_sync=lambda *a, **k: pytest.fail("credential write"), accounting=accounting)
    assert store._data.count_documents({}) == 0
    assert store.database.hermes_migrations.count_documents({}) == 0


def test_raced_durable_insert_never_overwrites_new_payload(deployment, monkeypatch):
    home, store, accounting = deployment
    plan = build_plan(home, "1", ["2"])
    original = store._put
    def raced(tenant, kind, key, payload, expected_revision=None):
        assert expected_revision == 0
        store._data.insert_one({"_id": store._data_id(tenant, kind, key), "tenant_id": tenant,
                                "kind": kind, "key": key, "payload": "raced-private", "revision": 1})
        return original(tenant, kind, key, payload, expected_revision)
    monkeypatch.setattr(store, "_put", raced)
    with pytest.raises(TenantStoreError, match="revision conflict"):
        execute_plan(store, Principal("1"), plan, credential_sync=lambda *a, **k: pytest.fail("credential write"), accounting=accounting)
    assert store._data.count_documents({}) == 1
    assert store._data.find_one()["payload"] == "raced-private"


@pytest.mark.parametrize("race_phase", ["provision", "access"])
def test_raced_profile_identity_never_receives_access(deployment, monkeypatch, race_phase):
    home, store, accounting = deployment
    plan = build_plan(home, "1", ["2"])
    unrelated = {"_id": "tg-2", "user_id": "99", "profile_id": "tg-2", "allowed": False}
    if race_phase == "provision":
        original = store.provision_user
        def provision(uid):
            if uid == "2":
                store._profiles.insert_one(unrelated)
            return original(uid)
        monkeypatch.setattr(store, "provision_user", provision)
    else:
        original = store._profiles.update_one
        def update(query, changes, *args, **kwargs):
            if query.get("_id") == "tg-2" and changes == {"$set": {"allowed": True}}:
                store._profiles.replace_one({"_id": "tg-2"}, unrelated)
            return original(query, changes, *args, **kwargs)
        monkeypatch.setattr(store._profiles, "update_one", update)
    with pytest.raises(TenantStoreError, match="Conflicting user profile during migration"):
        execute_plan(store, Principal("1"), plan, credential_sync=lambda *a, **k: pytest.fail("credential write"), accounting=accounting)
    assert store._profiles.find_one({"_id": "tg-2"}) == unrelated
    assert store._data.count_documents({}) == 0


@pytest.mark.parametrize("content", ["[not, a, mapping]\n", "invalid: [unterminated\n"])
def test_invalid_source_config_fails_closed(deployment, content):
    home, store, _accounting = deployment
    (home / "config.yaml").write_text(content)
    with pytest.raises(TenantStoreError, match="Cannot inventory provider configuration"):
        build_plan(home, "1", ["2"])
    assert store._data.count_documents({}) == 0


def test_exact_pending_plan_resumes_staged_sql_without_reinsert_or_duplicate_usage(deployment, monkeypatch):
    from agent.mongo_migration import MIGRATION_ID
    from agent.mongo_sql_cache import SQLCacheError
    from pymongo.errors import OperationFailure

    home, store, accounting = deployment
    plan = build_plan(home, "1", ["1", "2"])
    sync = lambda *a, **k: {"credential_records": 1}
    versions = store.database.hermes_sql_versions
    with monkeypatch.context() as patch:
        def fail(*a, **k):
            raise OperationFailure("synthetic failure before manifest publication", code=1234)
        patch.setattr(versions, "update_one", fail)
        with pytest.raises(SQLCacheError) as error:
            execute_plan(store, Principal("1"), plan, credential_sync=sync, accounting=accounting)
    assert error.value.stage == "stage_manifest"
    marker = store.database.hermes_migrations.find_one({"_id": MIGRATION_ID})
    assert marker["status"] == "pending" and marker["digest"] == plan["digest"]
    rows = list(store.database.hermes_sql_rows.find({}))
    durable = list(store._data.find({}))
    events = list(accounting.events.find({}))
    assert rows and events and durable
    assert store.database.hermes_sql_heads.count_documents({}) == 0

    # A different valid local inventory may not adopt this pending migration.
    memory = home / "memories/MEMORY.md"
    before = memory.read_bytes()
    memory.write_text("different valid source")
    changed = build_plan(home, "1", ["1", "2"])
    with pytest.raises(TenantStoreError, match="explicit reconciliation"):
        execute_plan(store, Principal("1"), changed, credential_sync=sync, accounting=accounting)
    memory.write_bytes(before)

    with monkeypatch.context() as patch:
        patch.setattr(store.database.hermes_sql_rows, "insert_many", lambda *a, **k: pytest.fail("staged rows must be reused"))
        receipt = execute_plan(store, Principal("1"), plan, credential_sync=sync, accounting=accounting)
    assert receipt["status"] == "complete" and receipt["digest"] == marker["digest"]
    assert receipt["sql_verified"] == 1 and receipt["usage_verified"] == 2
    assert list(store.database.hermes_sql_rows.find({})) == rows
    assert list(store._data.find({})) == durable
    assert list(accounting.events.find({})) == events
    assert store.database.hermes_sql_heads.find_one({})["revision"] == 1
    assert store.get(Principal("2"), "memory", "root:MEMORY.md") is None
    assert accounting.totals(Principal("1"))["input_tokens"] == 10
    assert accounting.totals(Principal("2"))["input_tokens"] == 20
