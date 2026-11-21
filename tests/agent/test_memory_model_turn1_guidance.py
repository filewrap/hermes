"""Behaviour contracts for the guidance-only turn-1 memory injection.

On the first turn of a session the system prompt carries memory GUIDANCE only — no
MEMORY.md / USER.md entry text. Later turns (and post-compression rebuilds) restore
the full blocks. Unknown turn counts fail open to the full blocks.
"""
from __future__ import annotations

from types import SimpleNamespace

from agent.system_prompt import _memory_parts


class _Store:
    """Minimal MemoryStore double: whole-file blocks for each target."""

    def __init__(self, entries):
        self._entries = entries

    def format_for_system_prompt(self, kind):
        text = self._entries.get(kind, "")
        return f"[{kind}] {text}" if text else None


def _agent(**overrides):
    base = dict(
        _memory_store=_Store({"memory": "USER FACT: prefers tea", "user": "NAME: Kailash"}),
        _memory_enabled=True,
        _user_profile_enabled=True,
        _memory_manager=None,
        _user_turn_count=1,
        valid_tool_names=["memory", "memory_search", "memory_get"],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_turn_1_carries_guidance_only():
    parts = _memory_parts(_agent(_user_turn_count=1))
    joined = "\n\n".join(parts)
    assert "Memory Recall" in joined
    assert "memory_search" in joined
    assert "USER FACT" not in joined
    assert "NAME: Kailash" not in joined


def test_turn_1_before_any_turn_counts_as_first():
    parts = _memory_parts(_agent(_user_turn_count=0))
    assert "USER FACT" not in "\n\n".join(parts)


def test_later_turns_restore_the_full_blocks():
    parts = _memory_parts(_agent(_user_turn_count=2))
    joined = "\n\n".join(parts)
    assert "USER FACT" in joined
    assert "NAME: Kailash" in joined
    assert "Memory Recall" not in joined


def test_unknown_turn_count_fails_open_to_full_blocks():
    agent = _agent()
    del agent._user_turn_count
    joined = "\n\n".join(_memory_parts(agent))
    assert "USER FACT" in joined


def test_turn_1_with_no_recall_tools_injects_nothing():
    parts = _memory_parts(_agent(_user_turn_count=1, valid_tool_names=["memory"]))
    assert parts == []


def test_no_store_injects_nothing():
    assert _memory_parts(_agent(_memory_store=None, _user_turn_count=1)) == []
