from concurrent.futures import ThreadPoolExecutor

import mongomock
import pytest

from agent.local_tenants import LocalTenantStore
from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError, _tenant_id

OWNER = "owner-id"


def make_mongo_store(tmp_path, monkeypatch):
    from agent import mongo_credentials

    monkeypatch.setattr(mongo_credentials, "_settings", lambda _home: object())
    db = mongomock.MongoClient().get_database("hermes")
    return MongoTenantStore(tmp_path, OWNER, database=db)


def test_local_authorization_generation_is_durable_and_access_shapes_unchanged(tmp_path):
    store = LocalTenantStore(tmp_path / "profile", OWNER)
    owner = Principal(OWNER)
    assert store.authorization_generation("alice") == 0
    assert store.get_access("alice") == {"allowed": False, "banned": False}

    store.allow_user(owner, "alice", True)
    assert store.authorization_generation("alice") == 1
    store.allow_user(owner, "alice", False)
    assert store.authorization_generation("alice") == 2
    store.allow_user(owner, "alice", True)
    store.ban_user(owner, "alice", True)
    store.ban_user(owner, "alice", False)
    assert store.authorization_generation("alice") == 5

    # Startup metadata changes are deliberately not authorization changes.
    store.start_user("alice")
    store.record_started_identity("alice", "alice_1", "Alice")
    assert store.authorization_generation("alice") == 5

    reopened = LocalTenantStore(tmp_path / "profile", OWNER)
    assert reopened.authorization_generation("alice") == 5
    assert reopened.get_access("alice") == {"allowed": True, "banned": False}


def test_local_two_instances_increment_generation_atomically(tmp_path):
    home = tmp_path / "profile"
    stores = [LocalTenantStore(home, OWNER), LocalTenantStore(home, OWNER)]
    owner = Principal(OWNER)

    def mutate(index):
        stores[index % 2].allow_user(owner, "parallel-user", True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(mutate, range(48)))
    assert stores[0].authorization_generation("parallel-user") == 48
    assert LocalTenantStore(home, OWNER).authorization_generation("parallel-user") == 48


def test_local_scoped_grant_revoke_and_regrant_each_advance_generation(tmp_path):
    store = LocalTenantStore(tmp_path / "profile", OWNER)
    owner = Principal(OWNER)
    store.start_user("90001")
    store.start_user("90002")
    store.grant_sudo(owner, "90001", "dm")
    admin = store.principal_for_scope("90001", "dm", "90001")

    store.grant_sudo(admin, "90002", "dm")
    assert store.authorization_generation("90002") == 1
    assert store.access_for_scope("90002", "dm", "90002") == {
        "allowed": True, "banned": False, "sudo": True, "started": True,
    }
    store.revoke_sudo(admin, "90002", "dm")
    assert store.authorization_generation("90002") == 2
    store.grant_sudo(admin, "90002", "dm")
    assert store.authorization_generation("90002") == 3
    assert store.get_access("90002") == {"allowed": False, "banned": False}


def test_local_legacy_generation_defaults_to_zero_and_corrupt_fails_closed(tmp_path):
    store = LocalTenantStore(tmp_path / "profile", OWNER)
    store.provision_user("legacy")
    assert store.authorization_generation("legacy") == 0

    store.provision_user("corrupt")
    store._profiles.update_one({"_id": _tenant_id("corrupt")},
                               {"$set": {"authorization_generation": True}})
    with pytest.raises(TenantStoreError, match="generation"):
        store.authorization_generation("corrupt")
    with pytest.raises(TenantStoreError):
        store.allow_user(Principal(OWNER), "corrupt", True)


def test_mongo_access_generation_atomic_and_legacy_compatible(tmp_path, monkeypatch):
    store = make_mongo_store(tmp_path, monkeypatch)
    owner = Principal(OWNER)
    assert store.authorization_generation("legacy") == 0
    store.allow_user(owner, "user", True)
    assert store.authorization_generation("user") == 1
    store.allow_user(owner, "user", False)
    store.allow_user(owner, "user", True)
    store.ban_user(owner, "user", True)
    store.ban_user(owner, "user", False)
    assert store.authorization_generation("user") == 5
    assert store.get_access("user") == {"allowed": True, "banned": False}


def test_mongo_corrupt_generation_fails_closed(tmp_path, monkeypatch):
    store = make_mongo_store(tmp_path, monkeypatch)
    store.provision_user("corrupt")
    ident = store.provision_user("corrupt")["_id"]
    store._profiles.update_one({"_id": ident}, {"$set": {"authorization_generation": 1.5}})
    with pytest.raises(TenantStoreError, match="generation"):
        store.authorization_generation("corrupt")
    with pytest.raises(TenantStoreError):
        store.allow_user(Principal(OWNER), "corrupt", True)


def test_local_group_generation_is_durable_and_separate_per_group(tmp_path):
    home = tmp_path / "profile"
    store = LocalTenantStore(home, OWNER)
    owner = Principal(OWNER)
    store.set_group_settings(owner, "-2001", {"enabled": True})
    store.set_group_settings(owner, "-2001", {"system_prompt": "policy update"})
    assert store.group_authorization_generation("-2001") == 2
    assert store.group_authorization_generation("-2002") == 0
    assert "authorization_generation" not in store.get_group_settings(owner, "-2001")
    reopened = LocalTenantStore(home, OWNER)
    assert reopened.group_authorization_generation("-2001") == 2


def test_group_settings_use_separate_generation_and_hide_internal_field(tmp_path, monkeypatch):
    store = make_mongo_store(tmp_path, monkeypatch)
    owner = Principal(OWNER)
    assert store.group_authorization_generation("-1001") == 0
    updated = store.set_group_settings(owner, "-1001", {"enabled": True})
    assert updated == {"_id": "-1001", "enabled": True}
    assert store.group_authorization_generation("-1001") == 1
    assert store.get_group_settings(owner, "-1001") == {"_id": "-1001", "enabled": True}
    store.set_group_settings(owner, "-1001", {"system_prompt": "updated"})
    assert store.group_authorization_generation("-1001") == 2
    assert store.group_authorization_generation("-1002") == 0

    store._groups.update_one({"_id": "broken"}, {"$set": {"authorization_generation": "bad"}}, upsert=True)
    with pytest.raises(TenantStoreError, match="generation"):
        store.group_authorization_generation("broken")
    with pytest.raises(TenantStoreError):
        store.set_group_settings(owner, "broken", {"enabled": True})
