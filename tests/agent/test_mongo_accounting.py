import mongomock
import pytest

from agent.mongo_accounting import MongoAccountingStore
from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.mongo_credentials._settings", lambda home: object())
    (tmp_path / ".env").write_text("MULTIUSER_ENABLED=true\nOWNER_TELEGRAM_ID=6802929470\n")
    db = mongomock.MongoClient().get_database("hermes")
    return MongoAccountingStore(tmp_path, database=db)


def test_turn_ledger_retry_conflict_and_isolation(store):
    owner, user = Principal("6802929470"), Principal("8495104954")
    store.tenants.allow_user(owner, user.user_id, True)
    usage = {"input_tokens": 8, "output_tokens": 3, "actual_cost_usd": 0.01}
    assert store.record_turn(user, "s1", "e1", usage)
    assert not store.record_turn(user, "s1", "e1", usage)
    assert store.totals(user)["input_tokens"] == 8
    assert store.totals(owner)["input_tokens"] == 0
    with pytest.raises(TenantStoreError):
        store.record_turn(user, "s1", "e1", {**usage, "input_tokens": 9})
    with pytest.raises(TenantStoreError):
        store.record_turn(Principal("12"), "s1", "e2", usage)


def test_absolute_snapshot_and_duplicate_event(store):
    owner = Principal("6802929470")
    values = {"input_tokens": 50, "output_tokens": 5}
    assert store.record_snapshot(owner, "s", "snap-1", values)
    assert not store.record_snapshot(owner, "s", "snap-1", values)
    assert store.totals(owner)["input_tokens"] == 50
    with pytest.raises(TenantStoreError):
        store.record_snapshot(owner, "s", "snap-1", {"input_tokens": 55})


def test_import_legacy_usage_is_idempotent_and_tenant_bound(store):
    owner = Principal("6802929470")
    usage = {"input_tokens": 7, "output_tokens": 2}
    assert store.import_legacy_usage(owner, owner, "old-session", usage)
    assert not store.import_legacy_usage(owner, owner, "old-session", usage)
    assert store.totals(owner)["input_tokens"] == 7
    store.tenants.allow_user(owner, "8495104954", True)
    assert store.totals(Principal("8495104954"))["input_tokens"] == 0


def test_owner_anchor_refuses_mismatched_database(store, tmp_path, monkeypatch):
    store.db["hermes_tenant_profiles"].insert_one({"_id": "owner", "user_id": "8495104954"})
    with pytest.raises(TenantStoreError):
        MongoTenantStore(tmp_path, "6802929470", database=store.db)
    with pytest.raises(TenantStoreError):
        store.owner_grant(Principal("6802929470"), "8495104954", credits=5)


def test_owner_credit_controls_and_user_self_mutation_denied(store):
    owner, user = Principal("6802929470"), Principal("8495104954")
    store.tenants.allow_user(owner, user.user_id, True)
    assert store.owner_grant(owner, user.user_id, credits=12, weight=2.5)["balance"] == 12
    assert store.balance(user) == {"balance": 12, "weight": 2.5}
    with pytest.raises(TenantStoreError):
        store.owner_grant(user, user.user_id, credits=99)
    store.tenants.ban_user(owner, user.user_id, True)
    with pytest.raises(TenantStoreError):
        store.balance(user)


def test_write_failure_does_not_fallback_or_claim_success(store, monkeypatch):
    user = Principal("6802929470")
    def fail(*args, **kwargs):
        raise RuntimeError("offline")
    monkeypatch.setattr(store.events, "insert_one", fail)
    monkeypatch.setattr(store.events, "find_one", lambda *a, **k: None)
    with pytest.raises(TenantStoreError):
        store.record_turn(user, "s", "retry-id", {"input_tokens": 1})
    assert store.totals(user)["input_tokens"] == 0


def test_ambiguous_event_commit_retry_repairs_aggregate(store, monkeypatch):
    user = Principal("6802929470")
    original = store.events.insert_one
    fired = False
    def commit_then_raise(doc):
        nonlocal fired
        result = original(doc)
        if not fired:
            fired = True
            raise RuntimeError("ambiguous response")
        return result
    monkeypatch.setattr(store.events, "insert_one", commit_then_raise)
    usage = {"input_tokens": 4}
    assert store.record_turn(user, "s", "ambiguous", usage) is False
    assert store.record_turn(user, "s", "ambiguous", usage) is False
    assert store.totals(user)["input_tokens"] == 4
