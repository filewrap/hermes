import mongomock
import pytest

from agent.mongo_accounting import MongoAccountingStore
from agent.mongo_tenants import Principal, TenantStoreError


def make_store(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.mongo_credentials._settings", lambda home: object())
    (tmp_path / ".env").write_text("MULTIUSER_ENABLED=true\nOWNER_TELEGRAM_ID=100\n")
    return MongoAccountingStore(tmp_path, database=mongomock.MongoClient().get_database("runtime"))


def test_snapshot_stale_order_and_delta_marker_survive(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    owner = Principal("100")
    store.record_turn(owner, "s", "delta-1", {"input_tokens": 2})
    store.record_snapshot(owner, "s", "snapshot-20", {"input_tokens": 20})
    assert store.record_snapshot(owner, "s", "snapshot-10", {"input_tokens": 10}) is False
    store.record_turn(owner, "s", "delta-1", {"input_tokens": 2})
    assert store.totals(owner)["input_tokens"] == 20


def test_legacy_ambiguous_insert_retry_repairs_aggregate(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    original = store.events.insert_one
    committed = False
    def commit_then_raise(doc):
        nonlocal committed
        result = original(doc)
        if doc.get("legacy_import") and not committed:
            committed = True
            raise RuntimeError("lost reply")
        return result
    monkeypatch.setattr(store.events, "insert_one", commit_then_raise)
    assert store.import_legacy_usage(Principal("100"), Principal("100"), "legacy", {"input_tokens": 4}) is False
    assert store.totals(Principal("100"))["input_tokens"] == 4


def test_grant_without_weight_preserves_prior_weight(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    owner = Principal("100")
    store.tenants.allow_user(owner, "200", True)
    store.owner_grant(owner, "200", credits=3, weight=2.5)
    assert store.owner_grant(owner, "200", credits=8)["weight"] == 2.5


def test_no_hardcoded_owner_default(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.mongo_credentials._settings", lambda home: object())
    with pytest.raises(TenantStoreError):
        MongoAccountingStore(tmp_path, database=mongomock.MongoClient().db)
