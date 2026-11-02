import logging

import mongomock
import pytest

from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.mongo_credentials._settings", lambda home: object())
    db = mongomock.MongoClient().get_database("hermes")
    return MongoTenantStore(tmp_path, "9000", database=db)


def test_provision_isolated_defaults_and_owner_explicit(store):
    profile = store.provision_user("123")
    assert profile == {"_id": "tg-123", "user_id": "123", "profile_id": "tg-123",
                       "allowed": False, "banned": False, "credits": 0, "weight": 1, "start_count": 0}
    profile["allowed"] = True
    assert store.provision_user("123")["allowed"] is False
    assert store.principal_tenant(Principal("9000")) == "owner"
    assert store.principal_tenant(Principal("abc")) == "u-ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert store.ensure_owner()["profile_id"] == "owner"
    assert store._profiles.count_documents({}) == 2


def test_tenant_scoped_data_cas_and_secret_restrictions(store):
    a, b = Principal("10"), Principal("11")
    store.allow_user(Principal("9000"), "10", True)
    store.allow_user(Principal("9000"), "11", True)
    rev = store.put(a, "memory", "x", {"v": 1})
    assert rev == 1 and store.get(a, "memory", "x") == {"v": 1}
    assert store.get(b, "memory", "x") is None
    assert store.put(a, "memory", "x", {"v": 2}, expected_revision=1) == 2
    with pytest.raises(TenantStoreError):
        store.put(a, "memory", "x", {}, expected_revision=1)
    with pytest.raises(TenantStoreError):
        store.get(a, "provider_credentials", "secret")
    with pytest.raises(TenantStoreError):
        store.put(a, "credits", "balance", 100)
    with pytest.raises(TenantStoreError):
        store.get(a, "policy", "x")


def test_owner_access_bans_start_group_and_migration(store):
    owner = Principal("9000")
    user = Principal("42")
    with pytest.raises(TenantStoreError):
        store.allow_user(user, "42", True)
    store.allow_user(owner, "42", True)
    store.ban_user(owner, "42", True)
    assert store.get_access("42") == {"allowed": True, "banned": True}
    assert store.start_user("42") == {"first_start": True, "start_count": 1}
    assert store.start_user("42") == {"first_start": False, "start_count": 2}
    store.ban_user(owner, "42", False)
    store.set_group_settings(user, "group", {"enabled": True, "default_model": "model-x"})
    assert store.get_group_settings(user, "group")["enabled"] is True
    with pytest.raises(TenantStoreError):
        store.set_group_settings(user, "group", {"provider_credentials": "x"})
    store.owner_put_tenant(owner, "tg-42", "settings", "theme", "dark")
    assert store.enumerate_tenant_data(owner, "tg-42")[0]["payload"] == "dark"
    with pytest.raises(TenantStoreError):
        store.enumerate_tenant_data(user, "tg-42")


def test_usage_idempotent_and_never_copied_to_owner(store):
    user = Principal("77")
    store.allow_user(Principal("9000"), "77", True)
    assert store.record_usage(user, "event-1", 3, 0.2) is True
    assert store.record_usage(user, "event-1", 3, 0.2) is False
    assert store._usage.count_documents({}) == 1
    assert store._usage.find_one({})["tenant_id"] == "tg-77"
    with pytest.raises(TenantStoreError):
        store.get(Principal("9000"), "usage", "event-1")


def test_access_controls_owner_profile_and_user_ledgers(store):
    owner = Principal("9000")
    assert store.get_access("9000") == {"allowed": True, "banned": False}
    assert store.start_user("9000") == {"first_start": True, "start_count": 1}
    with pytest.raises(TenantStoreError):
        store.allow_user(owner, "9000", False)
    with pytest.raises(TenantStoreError):
        store.ban_user(owner, "9000", True)
    pending = Principal("23")
    for action in (lambda: store.get(pending, "memory", "x"),
                   lambda: store.put(pending, "memory", "x", "bad"),
                   lambda: store.set_group_settings(pending, "g", {"enabled": True}),
                   lambda: store.get_group_settings(pending, "g"),
                   lambda: store.record_usage(pending, "x", 1, 1)):
        with pytest.raises(TenantStoreError):
            action()
    store.allow_user(owner, "23", True)
    store.ban_user(owner, "23", True)
    with pytest.raises(TenantStoreError):
        store.record_usage(pending, "x", 1, 1)
    with pytest.raises(TenantStoreError):
        store.get(pending, "profile", "x")


def test_usage_rejects_invalid_values_and_conflicting_duplicates(store):
    user = Principal("12")
    store.allow_user(Principal("9000"), "12", True)
    assert store.record_usage(user, "same", 2, 0.5)
    for units, cost in ((2, 0.6), (float("nan"), 0), (0, float("inf")), (-1, 0)):
        with pytest.raises(TenantStoreError):
            store.record_usage(user, "same", units, cost)
    for kind in ("usage", "credits", "weight", "providers"):
        with pytest.raises(TenantStoreError):
            store.put(user, kind, "k", 5)


def test_identical_put_is_noop_and_payload_validation(store):
    actor = Principal("5")
    store.allow_user(Principal("9000"), "5", True)
    assert store.put(actor, "memory", "k", {"a": [1, "v"]}) == 1
    assert store.put(actor, "memory", "k", {"a": [1, "v"]}, expected_revision=1) == 1
    with pytest.raises(TenantStoreError):
        store.put(actor, "memory", "k", {"$where": "bad"})
    with pytest.raises(TenantStoreError):
        store.put(actor, "memory", "k", float("nan"))


def test_start_is_atomic_under_concurrency(store):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: store.start_user("777"), range(40)))
    assert sum(result["first_start"] for result in results) == 1
    assert sorted(result["start_count"] for result in results) == list(range(1, 41))


def test_storage_logs_redacted_mongo_failure(store, caplog):
    secret_marker = "mongodb://private-user:private-password@private-host/private-db"

    class OperationFailure(Exception):
        code = 13

        def __str__(self):
            return f"not authorized; connection={secret_marker}"

    def raises(error):
        def raise_error():
            raise error
        return raise_error

    with caplog.at_level(logging.WARNING, logger="agent.mongo_tenants"):
        with pytest.raises(TenantStoreError):
            store._call(raises(OperationFailure()), operation="start_user_counter_update")
        with pytest.raises(TenantStoreError):
            store._call(raises(ModuleNotFoundError(secret_marker)),
                        operation="start_user_counter_update")

    assert "operation=start_user_counter_update" in caplog.text
    assert "error_type=OperationFailure" in caplog.text
    assert "failure_kind=authorization" in caplog.text
    assert "error_type=ModuleNotFoundError" in caplog.text
    assert "failure_kind=dependency_unavailable" in caplog.text
    assert "error_code=13" in caplog.text
    assert secret_marker not in caplog.text


def test_start_user_logs_missing_update_result_separately(store, caplog):
    def read_missing_count():
        updated = None
        return updated["start_count"]

    with caplog.at_level(logging.WARNING, logger="agent.mongo_tenants"):
        with pytest.raises(TenantStoreError):
            store._call(read_missing_count, operation="start_user_result")

    assert "operation=start_user_result" in caplog.text
    assert "error_type=TypeError" in caplog.text
    assert "failure_kind=invalid_update_result" in caplog.text


def test_continuation_scanner_returns_only_non_owner_profile_indexes(store):
    owner = Principal("9000")
    first = Principal("123")
    second = Principal("456")
    store.allow_user(owner, "123", True)
    store.allow_user(owner, "456", True)
    index_key = "telegram-continuations-v1"
    first_payload = {"schema": 1, "items": [{"id": "a" * 32, "kind": "bg"}]}
    second_payload = {"schema": 1, "items": [{"id": "b" * 32, "kind": "goal"}]}
    store.put(first, "profile", index_key, first_payload)
    store.put(second, "profile", index_key, second_payload)
    store.put(owner, "profile", index_key, {"schema": 1, "items": []})

    records = store.list_telegram_continuations()

    assert {record["user_id"] for record in records} == {"123", "456"}
    assert {record["payload"]["items"][0]["id"] for record in records} == {"a" * 32, "b" * 32}
    assert all(record["revision"] == 1 for record in records)


def test_continuation_indexes_do_not_cross_profile_roots_in_shared_mongo(tmp_path, monkeypatch):
    from gateway.tenant_tasks import _task_index_key

    monkeypatch.setattr("agent.mongo_credentials._settings", lambda home: object())
    database = mongomock.MongoClient().get_database("hermes")
    first_home = tmp_path / "profile-a"
    second_home = tmp_path / "profile-b"
    first = MongoTenantStore(first_home, "9000", database=database)
    second = MongoTenantStore(second_home, "9000", database=database)
    owner = Principal("9000")
    first.allow_user(owner, "123", True)
    first_key = _task_index_key(first_home)
    second_key = _task_index_key(second_home)
    first_payload = {"schema": 1, "items": [{"id": "a" * 32, "kind": "bg"}]}
    second_payload = {"schema": 1, "items": [{"id": "b" * 32, "kind": "goal"}]}
    first.put(Principal("123"), "profile", first_key, first_payload)
    second.put(Principal("123"), "profile", second_key, second_payload)

    first_records = first.list_telegram_continuations(first_key)
    second_records = second.list_telegram_continuations(second_key)

    assert [row["payload"] for row in first_records] == [first_payload]
    assert [row["payload"] for row in second_records] == [second_payload]
