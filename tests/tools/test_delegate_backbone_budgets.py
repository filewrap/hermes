"""Autonomous backbone budgets (DESIGN.md §3): per-run token cap, per-worker token cap,
per-worker iteration cap, spawn-time budget refusal.

Behaviour contracts: a worker stops at its own cap, a run stops at the run cap, a
spawn past a spent run budget is refused with a reason, and worker tokens land in
the run record once each (the coordinator's ledger is never double-counted).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _usage(prompt, out):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=out, total_tokens=prompt + out,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
        completion_tokens_details=None,
    )


def _agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from run_agent import AIAgent

    return AIAgent(
        api_key="k", base_url="https://inference-api.nousresearch.com/v1", provider="nous",
        api_mode="chat_completions", model="anthropic/claude-fable-5.1", session_id="t",
        platform="cli", quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False, enabled_toolsets=["file"],
    )


def _run_record(home, in_tokens, out_tokens):
    from gateway import autonomous_state

    record = autonomous_state.create_run(home, "sess", "goal")
    autonomous_state.checkpoint(home, record["run_id"], input_tokens=in_tokens, output_tokens=out_tokens)
    return record


def _begin_agent(**kw):
    agent = SimpleNamespace(
        _drain_pending_redirect=lambda: "",
        _checkpoint_mgr=SimpleNamespace(new_turn=lambda: None),
        _interrupt_requested=False,
        quiet_mode=True,
        _safe_print=lambda *a, **k: None,
        _touch_activity=lambda *a, **k: None,
        iteration_budget=SimpleNamespace(consume=lambda: True),
        _budget_grace_call=False,
        session_input_tokens=0,
        session_total_tokens=0,
    )
    for k, v in kw.items():
        setattr(agent, k, v)
    return agent


def _begin(agent):
    from agent.turn_iteration_prep import begin_iteration

    return begin_iteration(
        agent, messages=[], conversation_history=[], original_user_message="hi",
        api_call_count=0, interrupted=False, _turn_exit_reason="unknown",
    )


class TestWorkerIterationCap:
    """delegation.max_iterations_per_worker (100) wins; null falls back to max_iterations."""

    def test_new_key_wins_over_max_iterations(self):
        from tools.delegate_tool import _effective_worker_max_iterations

        assert _effective_worker_max_iterations(
            {"max_iterations_per_worker": 100, "max_iterations": 250}) == 100

    def test_null_falls_back_to_max_iterations(self):
        from tools.delegate_tool import _effective_worker_max_iterations

        assert _effective_worker_max_iterations(
            {"max_iterations_per_worker": None, "max_iterations": 250}) == 250

    def test_absent_keys_keep_the_old_default(self):
        from tools.delegate_tool import _effective_worker_max_iterations

        assert _effective_worker_max_iterations({}) == 250


class TestRunBudgetConfig:
    """autonomous.max_tokens_per_run reads through the real config loader."""

    def test_cap_from_temp_config_yaml(self, tmp_path):
        from gateway import autonomous_state

        (tmp_path / "config.yaml").write_text(
            "autonomous:\n  max_tokens_per_run: 5\n", encoding="utf-8")
        assert autonomous_state._config_max_tokens_per_run(tmp_path) == 5

    def test_cap_defaults_to_one_million(self, tmp_path):
        from gateway import autonomous_state

        assert autonomous_state._config_max_tokens_per_run(tmp_path) == 1_000_000

    def test_delegation_budget_keys_from_temp_config_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            "delegation:\n  max_tokens_per_worker: 12345\n  max_iterations_per_worker: 77\n",
            encoding="utf-8")
        from tools.delegate_tool_config import (
            _get_max_iterations_per_worker,
            _get_max_tokens_per_worker,
        )

        assert _get_max_tokens_per_worker() == 12345
        assert _get_max_iterations_per_worker() == 77


class TestSpawnRefusal:
    """A spawn past a spent run budget is refused with a reason, never silent."""

    def test_spent_run_budget_refuses_the_spawn(self, tmp_path):
        from gateway import autonomous_state
        import tools.delegate_tool as dt

        (tmp_path / "config.yaml").write_text(
            "autonomous:\n  max_tokens_per_run: 1000\n", encoding="utf-8")
        record = _run_record(tmp_path, 800, 200)
        parent = SimpleNamespace(
            _delegate_depth=0,
            _autonomous_run_home=str(tmp_path),
            _autonomous_run_id=record["run_id"],
        )
        out = json.loads(dt.delegate_task(tasks=[{"goal": "x"}], parent_agent=parent))
        assert "Budget limit" in out["error"]

    def test_under_budget_spawn_passes_the_gate(self, tmp_path):
        from gateway import autonomous_state
        import tools.delegate_tool as dt
        from tools.delegate_tool_routes import TaskRoute

        (tmp_path / "config.yaml").write_text(
            "autonomous:\n  max_tokens_per_run: 1000\n", encoding="utf-8")
        record = _run_record(tmp_path, 10, 5)
        parent = SimpleNamespace(
            _delegate_depth=0,
            _autonomous_run_home=str(tmp_path),
            _autonomous_run_id=record["run_id"],
        )
        route = TaskRoute(
            creds={
                "model": "m", "provider": None, "base_url": None, "api_key": None,
                "api_mode": None, "command": None, "args": None,
            },
            routing_cfg={},
        )
        with (
            patch.object(dt, "_routes_for", return_value=([route], None)),
            patch.object(dt, "_build_children", return_value=([], None)),
            patch.object(dt, "_run_batch", return_value={"ok": True}),
        ):
            out = dt.delegate_task(tasks=[{"goal": "x"}], parent_agent=parent)
        assert out == {"ok": True}


class TestWorkerTokenReporting:
    """Workers report each response's tokens to the run record, exactly once."""

    def test_worker_tokens_land_in_the_run_record(self, tmp_path, monkeypatch):
        from agent import turn_usage
        from gateway import autonomous_state
        from tools.delegate_tool_budget import report_worker_response_usage

        record = autonomous_state.create_run(tmp_path, "sess", "goal")
        agent = _agent(tmp_path, monkeypatch)
        try:
            agent._autonomous_run_home = str(tmp_path)
            agent._autonomous_run_id = record["run_id"]
            agent._on_response_usage = report_worker_response_usage
            resp = SimpleNamespace(usage=_usage(100, 7), id=None, model="m")
            turn_usage.record_response_usage(
                agent, resp, messages=[{"role": "user", "content": "hi"}],
                api_call_count=1, api_duration=0.2, compression_attempts=0,
                max_compression_attempts=3)
            turn_usage.record_response_usage(
                agent, SimpleNamespace(usage=_usage(50, 3), id=None, model="m"),
                messages=[{"role": "user", "content": "hi"}], api_call_count=2,
                api_duration=0.2, compression_attempts=0, max_compression_attempts=3)
        finally:
            agent.close()
        after = autonomous_state.load_run(tmp_path, record["run_id"])
        assert after["input_tokens"] == 150
        assert after["output_tokens"] == 10

    def test_coordinator_tokens_are_not_reported_twice(self, tmp_path, monkeypatch):
        # The coordinator has run context but no per-response hook: its tokens
        # reach the record only through checkpoint_tool, never here.
        from agent import turn_usage
        from gateway import autonomous_state

        record = autonomous_state.create_run(tmp_path, "sess", "goal")
        agent = _agent(tmp_path, monkeypatch)
        try:
            agent._autonomous_run_home = str(tmp_path)
            agent._autonomous_run_id = record["run_id"]
            turn_usage.record_response_usage(
                agent, SimpleNamespace(usage=_usage(100, 7), id=None, model="m"),
                messages=[{"role": "user", "content": "hi"}], api_call_count=1,
                api_duration=0.2, compression_attempts=0, max_compression_attempts=3)
        finally:
            agent.close()
        after = autonomous_state.load_run(tmp_path, record["run_id"])
        assert after["input_tokens"] == 0
        assert after["output_tokens"] == 0


class TestBudgetBreak:
    """The turn loop breaks at the top of the next iteration once a budget is spent."""

    def test_worker_cap_breaks_the_turn(self):
        agent = _begin_agent(session_total_tokens=250_000, _autonomous_worker_token_cap=250_000)
        verdict = _begin(agent)
        assert verdict.action == "break"
        assert verdict._turn_exit_reason == "autonomous_worker_budget_exceeded"

    def test_run_cap_breaks_the_turn(self):
        agent = _begin_agent(_autonomous_run_budget_check=lambda: True)
        verdict = _begin(agent)
        assert verdict.action == "break"
        assert verdict._turn_exit_reason == "autonomous_budget_exceeded"

    def test_under_budget_falls_through(self):
        agent = _begin_agent(
            session_total_tokens=10, _autonomous_worker_token_cap=250_000,
            _autonomous_run_budget_check=lambda: False)
        verdict = _begin(agent)
        assert verdict.action == "fallthrough"


class TestBudgetFailedRun:
    """A coordinator turn that breaks on the run budget marks the run failed."""

    def test_disarm_marks_the_run_failed_with_budget_reason(self, tmp_path):
        from gateway import autonomous_state
        from gateway import autonomous_turn

        record = autonomous_state.create_run(tmp_path, "sess", "goal")
        autonomous_state.start_run(tmp_path, record["run_id"])
        turn_runner = SimpleNamespace(
            _autonomous_armed=True, _autonomous_active=True,
            _autonomous_home=str(tmp_path), _autonomous_run_id=record["run_id"],
        )
        agent = SimpleNamespace(
            run_budget_seconds=None, _autonomous_budget_exceeded="run")
        with patch.object(autonomous_turn, "_schedule_unpin", lambda tr, p: None):
            autonomous_turn.disarm_conversation(turn_runner, agent, "sess", None)
        after = autonomous_state.load_run(tmp_path, record["run_id"])
        assert after["state"] == "failed"
        assert "budget" in (after.get("last_error") or "")
        assert autonomous_state.is_active(tmp_path) is False


class TestWorkerEntryNamesBudget:
    """A worker stopped by its budget names it in the result entry."""

    def test_budget_exit_reason_in_entry(self):
        from tools.delegate_tool_child_run import _build_result_entry

        child = SimpleNamespace(
            model="m", session_prompt_tokens=250_000, session_completion_tokens=10,
            session_estimated_cost_usd=0.0, session_cost_status=None,
            _delegate_role="leaf",
        )
        result = {
            "final_response": "did half the work", "completed": False,
            "turn_exit_reason": "autonomous_worker_budget_exceeded",
            "api_calls": 3, "messages": [],
        }
        entry = _build_result_entry(
            child, result, 0, 1.0, SimpleNamespace(schema=None, valid=None, errors=[], retries=0))
        assert entry["exit_reason"] == "budget"

    def test_plain_max_iterations_stays_max_iterations(self):
        from tools.delegate_tool_child_run import _build_result_entry

        child = SimpleNamespace(
            model="m", session_prompt_tokens=10, session_completion_tokens=10,
            session_estimated_cost_usd=0.0, session_cost_status=None,
            _delegate_role="leaf",
        )
        result = {
            "final_response": "did half the work", "completed": False,
            "turn_exit_reason": "max_iterations_reached(100/100)",
            "api_calls": 3, "messages": [],
        }
        entry = _build_result_entry(
            child, result, 0, 1.0, SimpleNamespace(schema=None, valid=None, errors=[], retries=0))
        assert entry["exit_reason"] == "max_iterations"
