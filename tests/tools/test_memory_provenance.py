"""Behaviour contracts for per-entry memory provenance (the sidecar next to
MEMORY.md / USER.md) and its retrieval surfaces: memory_search, memory_get,
/memories listing, and the /memory approve replay (owner provenance).
"""
from __future__ import annotations

import json

from tools.memory_search_tool import memory_get, memory_search
from tools.memory_tool import apply_memory_pending, format_memories_text, list_memories
from tools.memory_tool_store import PROVENANCE_FILENAME, MemoryStore


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    store = MemoryStore()
    store.load_from_disk()
    return store


def test_recorded_origin_survives_a_fresh_load(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.add("memory", "prefers tea over coffee")
    store.record_entry_origin("memory", "prefers tea over coffee", "agent", session_id="s1")
    assert (tmp_path / PROVENANCE_FILENAME).exists()

    fresh = MemoryStore()
    fresh.load_from_disk()
    assert fresh.entry_origin("memory", "prefers tea over coffee") == "agent"
    assert fresh.entry_origin("memory", "no such entry") is None


def test_unknown_origin_is_never_recorded_and_sidecar_corruption_is_inert(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.add("memory", "an entry")
    store.record_entry_origin("memory", "an entry", "bogus")
    assert store.entry_origin("memory", "an entry") is None
    (tmp_path / PROVENANCE_FILENAME).write_text("{not json", encoding="utf-8")
    fresh = MemoryStore()
    fresh.load_from_disk()  # must not raise
    assert fresh.entry_origin("memory", "an entry") is None


def test_first_writer_wins_for_adds_replace_reauthors(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.add("memory", "stable fact")
    store.record_entry_origin("memory", "stable fact", "owner")
    store.record_entry_origin("memory", "stable fact", "agent")
    assert store.entry_origin("memory", "stable fact") == "owner"
    store.record_entry_origin("memory", "stable fact", "agent", replace=True)
    assert store.entry_origin("memory", "stable fact") == "agent"


def test_remove_drops_the_record_and_load_prunes_stale_records(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.add("memory", "temporary fact")
    store.record_entry_origin("memory", "temporary fact", "agent")
    store.remove("memory", "temporary")
    store.drop_entry_origin("memory", "temporary fact")
    assert store.entry_origin("memory", "temporary fact") is None

    store.add("memory", "hand-edited away")
    store.record_entry_origin("memory", "hand-edited away", "agent")
    (tmp_path / "MEMORY.md").write_text("", encoding="utf-8")
    fresh = MemoryStore()
    fresh.load_from_disk()
    assert fresh.entry_origin("memory", "hand-edited away") is None
    assert json.loads((tmp_path / PROVENANCE_FILENAME).read_text(encoding="utf-8"))["entries"] == {}


def test_search_and_get_surface_the_tag(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.add("memory", "the capital of France is Paris")
    store.record_entry_origin("memory", "the capital of France is Paris", "agent")
    store.add("memory", "an untagged entry about France")
    hits = json.loads(memory_search(query="France", store=store))
    assert hits["success"] is True
    by_excerpt = {hit["excerpt"]: hit["origin_class"] for hit in hits["results"]}
    assert by_excerpt["the capital of France is Paris"] == "agent"
    assert by_excerpt["an untagged entry about France"] is None
    got = json.loads(memory_get(target="memory", index=1, store=store))
    assert got["origin_class"] == "agent"


def test_approve_replay_tags_owner(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    result = apply_memory_pending(
        {"action": "batch", "target": "memory",
         "operations": [{"action": "add", "content": "approved standing fact"}]},
        store)
    assert result["success"] is True
    assert store.entry_origin("memory", "approved standing fact") == "owner"


def test_memories_listing_carries_the_tag(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.add("memory", "listed fact")
    store.record_entry_origin("memory", "listed fact", "system")
    rows = list_memories(store)
    assert rows[0]["origin_class"] == "system"
    assert "[origin: system]" in format_memories_text(rows)
