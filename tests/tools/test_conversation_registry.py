"""Conversation-address registry: deterministic refs, durable round-trip, prune cap."""

import pytest

from tools import conversation_registry as registry


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "STORE_PATH", tmp_path / "conversations.json")
    return registry


def test_ref_is_deterministic_and_stable(store):
    ref = store.conv_ref("telegram", "-10042")
    assert ref.startswith("conv_") and len(ref) == 37
    assert store.conv_ref("telegram", "-10042") == ref
    assert store.conv_ref("telegram", "-10042", "7") != ref
    assert store.conv_ref("telegram", "-10043") != ref


def test_record_and_resolve_round_trip(store):
    ref = store.record_conversation("telegram", "-10042", type="group", name="Testing Agents")
    entry = store.resolve_ref(ref)
    assert entry["platform"] == "telegram"
    assert entry["chat_id"] == "-10042"
    assert entry["type"] == "group"
    assert entry["name"] == "Testing Agents"
    assert entry["thread_id"] is None
    assert store.lookup("telegram", "-10042")["ref"] == ref


def test_rerecord_updates_name_and_refreshes_last_seen(store):
    ref = store.record_conversation("telegram", "100", type="dm", name="Ann")
    updated = store.record_conversation("telegram", "100", type="dm", name="Ann Renamed")
    assert updated == ref
    assert store.resolve_ref(ref)["name"] == "Ann Renamed"


def test_unknown_ref_resolves_to_none(store):
    assert store.resolve_ref("conv_" + "0" * 32) is None
    assert store.resolve_ref("not-a-ref") is None
    assert store.resolve_ref("") is None


def test_list_orders_freshest_first_and_filters(store):
    store.record_conversation("telegram", "1", type="dm", name="Alpha")
    store.record_conversation("telegram", "2", type="dm", name="Beta")
    store.record_conversation("slack", "C0X", type="group", name="Gamma")
    rows = store.list_conversations()
    assert [row["name"] for row in rows] == ["Gamma", "Beta", "Alpha"]
    assert [row["name"] for row in store.list_conversations(platform="telegram")] == ["Beta", "Alpha"]
    assert [row["name"] for row in store.list_conversations(search="alp")] == ["Alpha"]
    assert [row["name"] for row in store.list_conversations(limit=1)] == ["Gamma"]


def test_corrupt_store_degrades_to_empty(store, tmp_path):
    path = tmp_path / "conversations.json"
    path.write_text("{not json", encoding="utf-8")
    assert store.list_conversations() == []
    assert store.resolve_ref("conv_" + "0" * 32) is None
    ref = store.record_conversation("telegram", "9", type="dm", name="Recovered")
    assert store.resolve_ref(ref)["name"] == "Recovered"


def test_prune_keeps_newest_at_the_cap(store, monkeypatch):
    monkeypatch.setattr(store, "_MAX_ENTRIES", 3)
    for chat_id in ("1", "2", "3", "4"):
        store.record_conversation("telegram", chat_id, type="dm", name=f"Chat {chat_id}")
    rows = store.list_conversations(limit=10)
    assert [row["chat_id"] for row in rows] == ["4", "3", "2"]
