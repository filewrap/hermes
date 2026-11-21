"""Behaviour contracts for the pre-compaction memory flush.

The flush input gate (network-sourced tool results never reach the fork), the
skip guards, the flush prompt, and the bounded wait — including the fork being
spawned with write origin ``compaction_flush``. The review runner itself is
stubbed at the ``agent.background_review`` seam; no LLM call runs here.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent import background_review
import agent.pre_compaction_memory_flush as flush_mod
from agent.pre_compaction_memory_flush import (
    build_flush_prompt,
    filter_flush_messages,
    flush_durable_memory_before_compaction,
)
from tools.registry import registry


def _agent(**overrides):
    base = dict(
        session_id="s1",
        _memory_store=SimpleNamespace(),
        _memory_enabled=True,
        _user_profile_enabled=True,
        _persist_disabled=False,
        skip_background_review=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def network_tools():
    registry.register(
        name="flush_net_tool", toolset="test", schema={"name": "x", "description": "x"},
        handler=lambda args, **kw: "{}", result_content_source="network")
    registry.register(
        name="flush_local_tool", toolset="test", schema={"name": "x", "description": "x"},
        handler=lambda args, **kw: "{}", result_content_source=None)
    try:
        yield
    finally:
        registry.deregister("flush_net_tool")
        registry.deregister("flush_local_tool")


def _messages():
    return [
        {"role": "user", "content": "remember I prefer tea"},
        {"role": "assistant", "content": "noted",
         "tool_calls": [
             {"id": "c1", "function": {"name": "flush_net_tool", "arguments": "{}"}},
             {"id": "c2", "function": {"name": "flush_local_tool", "arguments": "{}"}},
             {"id": "c3", "function": {"name": "unknown_tool", "arguments": "{}"}},
         ]},
        {"role": "tool", "tool_call_id": "c1", "content": "web page says tea is great"},
        {"role": "tool", "tool_call_id": "c2", "content": "local file listing"},
        {"role": "tool", "tool_call_id": "c3", "content": "unattributable result"},
        {"role": "assistant", "content": "done"},
    ]


def test_filter_drops_only_network_tool_results(network_tools):
    kept = filter_flush_messages(_messages())
    contents = [m.get("content") for m in kept]
    assert "web page says tea is great" not in contents
    assert "local file listing" in contents
    assert "unattributable result" in contents
    assert "remember I prefer tea" in contents


def test_filter_keeps_everything_without_tool_calls():
    plain = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert filter_flush_messages(plain) == plain


def test_flush_prompt_names_the_pre_compaction_job_and_the_provenance_rule():
    prompt = build_flush_prompt(_agent())
    assert "Pre-compaction memory flush" in prompt
    assert "network" in prompt
    assert "Review the conversation above" in prompt


def test_flush_skips_without_a_store_or_when_disabled():
    assert flush_durable_memory_before_compaction(
        _agent(_memory_store=None), _messages())["reason"] == "no_memory_store"
    assert flush_durable_memory_before_compaction(
        _agent(_memory_enabled=False, _user_profile_enabled=False),
        _messages())["reason"] == "memory_disabled"
    assert flush_durable_memory_before_compaction(
        _agent(_persist_disabled=True), _messages())["reason"] == "persist_disabled"
    assert flush_durable_memory_before_compaction(
        _agent(skip_background_review=True), _messages())["reason"] == "review_skipped"
    assert flush_durable_memory_before_compaction(
        _agent(), [])["reason"] == "no_messages"


def test_flush_spawns_the_fork_as_compaction_flush(monkeypatch):
    seen = {}

    def fake_runner(agent, messages_snapshot, prompt, task_cfg=None, review_run=None,
                    review_memory=False, explicit=False, write_origin="background_review"):
        seen["write_origin"] = write_origin
        seen["review_memory"] = review_memory
        seen["prompt"] = prompt

    monkeypatch.setattr(background_review, "_run_review_in_thread", fake_runner)
    result = flush_durable_memory_before_compaction(_agent(), _messages(), join_timeout_s=10)
    assert result == {"flushed": True, "reason": "completed", "completed_in_time": True}
    assert seen["write_origin"] == "compaction_flush"
    assert seen["review_memory"] is True
    assert "Pre-compaction memory flush" in seen["prompt"]


def test_flush_times_out_but_never_blocks_compression(monkeypatch):
    gate = threading.Event()

    def blocking_runner(agent, messages_snapshot, prompt, task_cfg=None, review_run=None,
                        review_memory=False, explicit=False, write_origin="background_review"):
        gate.wait(timeout=30)

    monkeypatch.setattr(background_review, "_run_review_in_thread", blocking_runner)
    result = flush_durable_memory_before_compaction(_agent(), _messages(), join_timeout_s=0.2)
    assert result == {"flushed": True, "reason": "timed_out", "completed_in_time": False}
    gate.set()


def test_flush_stops_waiting_when_the_fence_cancels(monkeypatch):
    gate = threading.Event()

    def blocking_runner(agent, messages_snapshot, prompt, task_cfg=None, review_run=None,
                        review_memory=False, explicit=False, write_origin="background_review"):
        gate.wait(timeout=30)

    monkeypatch.setattr(background_review, "_run_review_in_thread", blocking_runner)
    fence = SimpleNamespace(is_cancelled=True)
    result = flush_durable_memory_before_compaction(
        _agent(), _messages(), commit_fence=fence, join_timeout_s=30)
    assert result["completed_in_time"] is False
    gate.set()


def test_flush_start_failure_releases_the_review_slot(monkeypatch):
    monkeypatch.setattr(
        "agent.turn_finalizer._clone_background_review_messages",
        lambda messages: (_ for _ in ()).throw(RuntimeError("clone blew up")),
    )
    agent = _agent()
    result = flush_durable_memory_before_compaction(agent, _messages())
    assert result["flushed"] is False
    assert result["reason"].startswith("start_failed")
    # The slot was released: a fresh review run can be prepared afterwards.
    assert background_review.prepare_background_review_run(agent) is not None
