"""Tests for tools/memory_search_tool.py — FTS5 memory retrieval (F1).

Behaviour contracts: a fact saved via the memory tool is findable by search
with its source file+line; memory_get returns a bounded excerpt with
truncation/continuation info; hostile query syntax never reaches MATCH raw.
"""

import json
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import (
    INLINE_TOOL_EXECUTORS, INVOKE_TOOL_PRE_MEMORY_MANAGER_NAMES, InlineToolContext)
from tools.memory_tool import MemoryStore
from tools.memory_search_tool import MEMORY_GET_SCHEMA, MEMORY_SEARCH_SCHEMA
from tools.memory_tool_store import ENTRY_DELIMITER
from tools.registry import registry
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    return MemoryStore(memory_char_limit=5000, user_char_limit=3000)


def _add(store, target, content):
    result = store.add(target, content)
    assert result["success"], result
    return result


def _search(store, **args):
    return json.loads(registry.get_entry("memory_search").handler(args, store=store))


def _get(store, **args):
    return json.loads(registry.get_entry("memory_get").handler(args, store=store))


# =========================================================================
# memory_search
# =========================================================================

class TestMemorySearch:
    def test_finds_fact_saved_via_memory_tool_with_source_line(self, store):
        _add(store, "memory", "Deployment checklist lives in the runbook.")
        result = _search(store, query="deployment checklist")
        assert result["success"] and result["count"] == 1, result
        hit = result["results"][0]
        assert hit["target"] == "memory" and hit["index"] == 1
        assert hit["source"] == "MEMORY.md:1"
        assert "Deployment checklist" in hit["excerpt"]
        assert hit["excerpt_truncated"] is False

    def test_second_entry_source_line_counts_delimiter_and_entry_lines(self, store):
        _add(store, "memory", "First entry\nspans three\nlines.")
        _add(store, "memory", "Second entry mentions the release process.")
        result = _search(store, query="release process")
        assert result["count"] == 1, result
        assert result["results"][0]["source"] == "MEMORY.md:5"

    def test_target_scoping(self, store):
        _add(store, "memory", "The deploy runbook is canonical.")
        _add(store, "user", "User prefers deploys on Fridays.")
        assert [h["target"] for h in _search(store, query="deploy", target="memory")["results"]] == ["memory"]
        assert [h["target"] for h in _search(store, query="deploy", target="user")["results"]] == ["user"]
        assert len(_search(store, query="deploy", target="all")["results"]) == 2

    def test_phrase_and_or_queries(self, store):
        _add(store, "memory", "The release process needs two approvals.")
        _add(store, "memory", "Rollback starts with the database snapshot.")
        assert _search(store, query='"release process"')["count"] == 1
        assert _search(store, query="release OR rollback")["count"] == 2
        assert _search(store, query="release snapshot")["count"] == 0

    def test_prefix_match(self, store):
        _add(store, "memory", "Deployment checklist lives in the runbook.")
        assert _search(store, query="deploy")["count"] == 1

    def test_no_match_empty_and_punctuation_queries(self, store):
        _add(store, "memory", "Deployment checklist lives in the runbook.")
        result = _search(store, query="nonexistent topic")
        assert result["success"] and result["count"] == 0 and "No matching" in result["message"]
        assert _search(store, query="")["success"] is False
        assert _search(store, query="   ")["success"] is False
        assert _search(store, query="::: ***")["success"] is False

    def test_hostile_query_syntax_never_reaches_match_raw(self, store):
        _add(store, "memory", "Deployment checklist lives in the runbook.")
        _add(store, "memory", "Unrelated note about gardening.")
        for query in ('"deploy', 'deploy" OR "x', "deploy: * (", "deploy NEAR/2 runbook",
                      '") OR ("1"="1', "deploy; DROP TABLE memory_fts"):
            result = _search(store, query=query)
            assert result["success"], (query, result)
            assert result["count"] <= 1, (query, result)

    def test_max_results_clamped(self, store):
        for i in range(5):
            _add(store, "memory", f"Deploy note number {i}.")
        assert len(_search(store, query="deploy", max_results=2)["results"]) == 2
        assert len(_search(store, query="deploy", max_results=99)["results"]) == 5

    def test_long_entry_excerpt_truncates_and_names_get(self, store):
        _add(store, "memory", "Deploy note. " * 60)
        result = _search(store, query="deploy")
        hit = result["results"][0]
        assert hit["excerpt_truncated"] is True and hit["entry_chars"] > len(hit["excerpt"])
        assert "memory_get" in result["message"]

    def test_disabled_target_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
        store = MemoryStore(memory_char_limit=5000, user_char_limit=3000,
                            memory_enabled=True, user_profile_enabled=False)
        _add(store, "memory", "Deployment checklist lives in the runbook.")
        assert _search(store, query="deploy", target="user")["success"] is False
        assert _search(store, query="deploy", target="all")["count"] == 1


# =========================================================================
# memory_get
# =========================================================================

class TestMemoryGet:
    def test_bounded_excerpt_with_continuation_round_trip(self, store):
        entry = ("Deploy note. " * 40).strip()
        _add(store, "memory", entry)
        first = _get(store, target="memory", index=1, max_chars=100)
        assert first["success"] and first["truncated"] is True, first
        assert first["source"] == "MEMORY.md:1" and first["entry_chars"] == len(entry)
        assert first["next_start_char"] == 100
        rest = _get(store, target="memory", index=1, start_char=first["next_start_char"])
        assert rest["success"] and rest["truncated"] is False
        assert first["excerpt"] + rest["excerpt"] == entry

    def test_full_short_entry(self, store):
        _add(store, "memory", "Deployment checklist lives in the runbook.")
        result = _get(store, target="memory", index=1)
        assert result["success"] and result["truncated"] is False
        assert result["excerpt"] == "Deployment checklist lives in the runbook."
        assert result["next_start_char"] is None

    def test_out_of_range_missing_and_wrong_target(self, store):
        _add(store, "memory", "Deployment checklist lives in the runbook.")
        assert _get(store, target="memory", index=2)["success"] is False
        assert _get(store, target="memory", index=0)["success"] is False
        assert _get(store, target="memory")["success"] is False
        assert _get(store, target="all", index=1)["success"] is False
        assert _get(store, target="bogus", index=1)["success"] is False

    def test_store_without_store_kwarg_is_unavailable(self):
        assert json.loads(registry.get_entry("memory_search").handler({"query": "x"}))["success"] is False
        assert json.loads(registry.get_entry("memory_get").handler({"target": "memory", "index": 1}))["success"] is False


# =========================================================================
# Registration, toolset, inline path
# =========================================================================

class TestMemorySearchWiring:
    def test_registered_in_memory_toolset_and_core(self):
        for name in ("memory_search", "memory_get"):
            entry = registry.get_entry(name)
            assert entry is not None and entry.toolset == "memory"
            assert name in TOOLSETS["memory"]["tools"]
            assert name in _HERMES_CORE_TOOLS
        assert registry.get_entry("memory_search").check_fn is not None

    def test_inline_executors_read_live_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store = MemoryStore(memory_char_limit=5000, user_char_limit=3000)
        agent = SimpleNamespace(_memory_store=store)
        ctx = InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")
        assert "memory_search" in INLINE_TOOL_EXECUTORS
        assert "memory_get" in INLINE_TOOL_EXECUTORS
        assert {"memory_search", "memory_get"} <= INVOKE_TOOL_PRE_MEMORY_MANAGER_NAMES
        _add(store, "memory", "Deployment checklist lives in the runbook.")
        found = json.loads(INLINE_TOOL_EXECUTORS["memory_search"](agent, {"query": "deployment"}, ctx))
        assert found["success"] and found["count"] == 1, found
        assert found["results"][0]["source"] == "MEMORY.md:1"
        read = json.loads(INLINE_TOOL_EXECUTORS["memory_get"](agent, {"target": "memory", "index": 1}, ctx))
        assert read["success"] and "Deployment checklist" in read["excerpt"]
