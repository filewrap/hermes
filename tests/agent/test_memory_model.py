"""Behaviour contracts for agent/memory_model.py — the durable-memory model.

Provenance (MemoryEntryOrigin.originClass), the guidance-only turn-1 prompt,
and the dreaming promotion gates (Light -> REM -> Deep) with the ported weights and
thresholds. Pure functions: no I/O, no LLM, no scheduler.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone, UTC
from types import SimpleNamespace

from agent.background_review import build_memory_write_metadata
from agent.memory_model import (
    ORIGIN_CLASS_AGENT,
    ORIGIN_CLASS_OWNER,
    ORIGIN_CLASS_SYSTEM,
    ORIGIN_CLASS_UNTRUSTED,
    PromotionCandidate,
    committed_memory_write_entries,
    deep_promotion_candidates,
    is_consolidation_candidate_eligible,
    is_first_memory_turn,
    is_promotion_origin_blocked,
    light_stage_candidates,
    memory_guidance_block,
    rank_promotion_candidates,
    rem_consider_candidates,
    resolve_memory_origin_class,
    score_promotion_candidate,
)
from agent.turn_taint import observe_tool_outcome, reset_turn_taint
from tools.registry import registry

NOW = datetime(2026, 11, 11, tzinfo=UTC)
NOW_MS = NOW.timestamp() * 1000.0


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _agent(**overrides):
    base = dict(session_id="s1", _parent_session_id="", platform="cli")
    base.update(overrides)
    return SimpleNamespace(**base)


def _candidate(**overrides):
    base = dict(
        key="k1", snippet="a durable fact", origin_class="agent", session_kind="interactive",
        recall_count=3, daily_count=0, grounded_count=3, avg_score=1.0, max_score=1.0,
        unique_queries=3, recall_days=[_iso(2)[:10], _iso(1)[:10], _iso(0)[:10]],
        concept_tags=[], first_recalled_at=_iso(2), last_recalled_at=_iso(0),
    )
    base.update(overrides)
    return PromotionCandidate(**base)


# -- Provenance resolver ------------------------------------------------------


def test_resolver_defaults_to_agent_on_a_clean_foreground_turn():
    assert resolve_memory_origin_class(_agent()) == ORIGIN_CLASS_AGENT


def test_resolver_reports_untrusted_on_a_tainted_turn():
    agent = _agent()
    registry.register(
        name="model_net_probe", toolset="test", schema={"name": "x", "description": "x"},
        handler=lambda args, **kw: "{}", result_content_source="network")
    try:
        observe_tool_outcome(agent, "model_net_probe")
        assert resolve_memory_origin_class(agent) == ORIGIN_CLASS_UNTRUSTED
    finally:
        registry.deregister("model_net_probe")
        reset_turn_taint(agent)


def test_resolver_reports_system_for_background_forks_and_cron():
    assert resolve_memory_origin_class(_agent(), write_origin="background_review") == ORIGIN_CLASS_SYSTEM
    assert resolve_memory_origin_class(_agent(), write_origin="background_curator") == ORIGIN_CLASS_SYSTEM
    assert resolve_memory_origin_class(_agent(), write_origin="compaction_flush") == ORIGIN_CLASS_SYSTEM
    assert resolve_memory_origin_class(_agent(_memory_write_origin="background_review")) == ORIGIN_CLASS_SYSTEM
    assert resolve_memory_origin_class(_agent(platform="cron")) == ORIGIN_CLASS_SYSTEM


def test_resolver_never_infers_owner():
    # Every resolvable source reports agent/untrusted/system; "owner" only ever comes
    # from the /memory approve replay, never from turn state.
    for kwargs in ({}, {"platform": "cron"}, {"_memory_write_origin": "background_review"}):
        assert resolve_memory_origin_class(_agent(**kwargs)) != ORIGIN_CLASS_OWNER
    agent = _agent()
    registry.register(
        name="model_net_probe2", toolset="test", schema={"name": "x", "description": "x"},
        handler=lambda args, **kw: "{}", result_content_source="network")
    try:
        observe_tool_outcome(agent, "model_net_probe2")
        assert resolve_memory_origin_class(agent) != ORIGIN_CLASS_OWNER
    finally:
        registry.deregister("model_net_probe2")
        reset_turn_taint(agent)


def test_metadata_carries_the_full_origin_set():
    assert build_memory_write_metadata(_agent())["origin_class"] == ORIGIN_CLASS_AGENT
    assert build_memory_write_metadata(
        _agent(), write_origin="background_review")["origin_class"] == ORIGIN_CLASS_SYSTEM
    assert build_memory_write_metadata(
        _agent(), write_origin="compaction_flush")["origin_class"] == ORIGIN_CLASS_SYSTEM


# -- Turn-1 guidance ----------------------------------------------------------


def test_first_turn_detection():
    assert is_first_memory_turn(_agent(_user_turn_count=0))
    assert is_first_memory_turn(_agent(_user_turn_count=1))
    assert not is_first_memory_turn(_agent(_user_turn_count=2))
    assert not is_first_memory_turn(_agent())  # unknown count fails open to full injection


def test_guidance_names_only_tools_on_the_surface():
    both = memory_guidance_block(_agent(valid_tool_names=["memory_search", "memory_get"]))
    assert "## Memory Recall" in both
    assert "memory_search" in both and "memory_get" in both
    assert "not preloaded" in both
    search_only = memory_guidance_block(_agent(valid_tool_names=["memory_search"]))
    assert "memory_search" in search_only and "memory_get" not in search_only
    assert memory_guidance_block(_agent(valid_tool_names=["memory"])) == ""
    assert memory_guidance_block(_agent(valid_tool_names=[])) == ""


# -- Promotion gates ----------------------------------------------------------


def test_origin_block_covers_untrusted_and_system_only():
    assert is_promotion_origin_blocked(_candidate(origin_class="untrusted"))
    assert is_promotion_origin_blocked(_candidate(origin_class="system"))
    assert not is_promotion_origin_blocked(_candidate(origin_class="owner"))
    assert not is_promotion_origin_blocked(_candidate(origin_class="agent"))
    assert not is_promotion_origin_blocked(_candidate(origin_class=None))


def test_consolidation_eligibility_needs_trusted_origin_and_interactive_kind():
    assert is_consolidation_candidate_eligible(_candidate(origin_class="owner"))
    assert is_consolidation_candidate_eligible(_candidate(origin_class="agent", session_kind=None))
    assert not is_consolidation_candidate_eligible(_candidate(origin_class="agent", session_kind="cron"))
    assert not is_consolidation_candidate_eligible(_candidate(origin_class="untrusted"))
    assert not is_consolidation_candidate_eligible(_candidate(origin_class="system"))


def test_strong_candidate_scores_at_or_above_the_deep_threshold():
    # The ported calibration: a 3-day / 3-query durable fact scores ~0.75.
    components = score_promotion_candidate(_candidate(), now_ms=NOW_MS)
    assert components["score"] >= 0.75


def test_ranking_drops_under_signalled_blocked_and_promoted_candidates():
    strong = _candidate(key="strong")
    weak = _candidate(key="weak", recall_count=1, unique_queries=1, avg_score=0.5, grounded_count=0)
    blocked = _candidate(key="blocked", origin_class="untrusted")
    done = _candidate(key="done", promoted_at=_iso(0))
    ranked = rank_promotion_candidates([weak, blocked, done, strong], now_ms=NOW_MS)
    assert [candidate.key for candidate, _ in ranked] == ["strong"]
    assert rank_promotion_candidates([done], now_ms=NOW_MS, include_promoted=True)


def test_ranking_orders_by_score_then_recall():
    first = _candidate(key="first", recall_count=6, grounded_count=6)
    second = _candidate(key="second")
    ranked = rank_promotion_candidates([second, first], now_ms=NOW_MS)
    assert [candidate.key for candidate, _ in ranked] == ["first", "second"]


def test_light_rem_deep_pipeline_shape():
    fresh = _candidate(key="fresh")
    stale = _candidate(key="stale", last_recalled_at=_iso(30), first_recalled_at=_iso(30))
    promoted = _candidate(key="promoted", promoted_at=_iso(0))
    staged = light_stage_candidates([fresh, stale, promoted], now_ms=NOW_MS)
    assert [candidate.key for candidate in staged] == ["fresh"]
    staged_keys = {candidate.key for candidate in staged}
    # REM reads only what Light staged — a fresh rescan must not smuggle the stale one in.
    considered = rem_consider_candidates([fresh, stale], light_staged_keys=staged_keys)
    assert [candidate.key for candidate in considered] == ["fresh"]
    deep = deep_promotion_candidates(considered, now_ms=NOW_MS)
    assert [candidate.key for candidate, _ in deep] == ["fresh"]


# -- Committed-write extraction ------------------------------------------------


def test_committed_entries_single_add_and_staged_gate():
    args = {"target": "memory", "operations": [{"action": "add", "content": "hello"}]}
    rows = committed_memory_write_entries(args, {"success": True, "done": True})
    assert rows == [{"target": "memory", "action": "add", "entry": "hello", "previous_entry": ""}]
    assert committed_memory_write_entries(args, {"success": True, "staged": True}) == []
    assert committed_memory_write_entries(args, {"success": False}) == []
    assert committed_memory_write_entries(args, "not json") == []


def test_committed_entries_batch_patch_and_remove_use_store_results():
    args = {"target": "memory", "operations": [
        {"action": "patch", "old_text": "old", "new_text": "new"},
        {"action": "remove", "old_text": "gone"},
    ]}
    result = {
        "success": True,
        "patched_entries_after": {"1": "patched whole entry"},
        "patched_entries": {"1": "original whole entry"},
        "removed_entries": {"2": "removed whole entry"},
    }
    rows = committed_memory_write_entries(args, result)
    assert rows == [
        {"target": "memory", "action": "patch", "entry": "patched whole entry",
         "previous_entry": "original whole entry"},
        {"target": "memory", "action": "remove", "entry": "removed whole entry", "previous_entry": ""},
    ]


def test_committed_entries_single_replace_and_remove_use_store_results():
    replace_args = {"target": "user", "action": "replace", "old_text": "a", "new_text": "b"}
    rows = committed_memory_write_entries(
        replace_args, json.dumps({"success": True, "replaced_entry": "a"}))
    assert rows == [{"target": "user", "action": "replace", "entry": "b", "previous_entry": "a"}]
    remove_args = {"target": "user", "action": "remove", "old_text": "a"}
    rows = committed_memory_write_entries(remove_args, {"success": True, "removed_entry": "a"})
    assert rows == [{"target": "user", "action": "remove", "entry": "a", "previous_entry": ""}]
