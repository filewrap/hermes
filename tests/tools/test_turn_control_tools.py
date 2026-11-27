"""Tests for the F3 turn-control tools (sessions_yield + agents_wait).

Live-check shape, in the sandbox: register two runs, settle them, and
agents_wait joins them without polling; sessions_yield claims the pending
runs and arms the deterministic turn end. All run zero LLM calls. The
registry is process-local module state, so every test uses its own session
id — runs from another test are never owned by this test's agent.
"""

import json
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from tools.process_registry_notifications import format_process_notification
from tools.subagent_run_registry import (
    MAX_WAIT_IDS, await_runs, claim_yield, pending_yielded_runs, register_run, settle_run,
)


@pytest.fixture
def session():
    return f"s-test-{uuid.uuid4().hex[:8]}"


def _agent(session_id):
    return SimpleNamespace(session_id=session_id, _interrupt_requested=False, _yield_requested=None)


def _ctx():
    return InlineToolContext(effective_task_id="t-1", tool_call_id="call-1")


def _register(run_id, session_id, **kw):
    return register_run(run_id, goal=kw.pop("goal", f"task {run_id}"),
                        owner_agent_session_id=session_id, **kw)


def _call(name, agent, args):
    return json.loads(INLINE_TOOL_EXECUTORS[name](agent, args, _ctx()))


# =========================================================================
# Run registry: settle + await (event-driven, no polling)
# =========================================================================

class TestAwaitRuns:
    def test_settled_before_wait_returns_immediately(self, session):
        _register("sa-wait-1", session)
        settle_run("sa-wait-1", {"status": "completed", "summary": "done A", "duration_seconds": 1.5})
        state = await_runs(_agent(session), ["sa-wait-1"], timeout_seconds=5)
        assert [c["runId"] for c in state["completed"]] == ["sa-wait-1"]
        assert state["completed"][0]["status"] == "done"
        assert state["completed"][0]["result"] == "done A"
        assert state["completed"][0]["runtimeMs"] == 1500.0
        assert state["pending"] == [] and state["errors"] == []

    def test_wait_wakes_on_settle_well_before_timeout(self, session):
        _register("sa-wait-2a", session)
        _register("sa-wait-2b", session)
        threading.Timer(0.2, lambda: settle_run(
            "sa-wait-2a", {"status": "completed", "summary": "first"})).start()
        started = time.monotonic()
        state = await_runs(_agent(session), ["sa-wait-2a", "sa-wait-2b"], timeout_seconds=10)
        assert time.monotonic() - started < 5  # woke on the settle, not the deadline
        assert [c["runId"] for c in state["completed"]] == ["sa-wait-2a"]
        assert state["pending"] == ["sa-wait-2b"]

    def test_await_results_joins_all(self, session):
        _register("sa-wait-3a", session)
        _register("sa-wait-3b", session)
        threading.Timer(0.1, lambda: settle_run(
            "sa-wait-3a", {"status": "completed", "summary": "A"})).start()
        threading.Timer(0.2, lambda: settle_run(
            "sa-wait-3b", {"status": "failed", "summary": "", "error": "boom"})).start()
        state = await_runs(_agent(session), ["sa-wait-3b", "sa-wait-3a"], await_results=True)
        assert [c["runId"] for c in state["completed"]] == ["sa-wait-3a", "sa-wait-3b"]
        assert state["completed"][1]["status"] == "failed"
        assert state["completed"][1]["error"] == "boom"
        assert state["pending"] == []

    def test_timeout_returns_pending(self, session):
        _register("sa-wait-4", session)
        state = await_runs(_agent(session), ["sa-wait-4"], timeout_seconds=0.1)
        assert state["completed"] == [] and state["pending"] == ["sa-wait-4"]

    def test_interrupt_breaks_the_wait(self, session):
        _register("sa-wait-5", session)
        agent = _agent(session)
        threading.Timer(0.1, lambda: setattr(agent, "_interrupt_requested", True)).start()
        state = await_runs(agent, ["sa-wait-5"], await_results=True)
        assert state["pending"] == ["sa-wait-5"]

    def test_unknown_and_foreign_runs(self, session):
        _register("sa-wait-6", "s-other")
        state = await_runs(_agent(session), ["sa-missing", "sa-wait-6"], timeout_seconds=0)
        assert state["errors"] == [
            {"runId": "sa-missing", "error": "not_found"},
            {"runId": "sa-wait-6", "error": "not_owner"},
        ]
        assert state["completed"] == [] and state["pending"] == []

    def test_first_settle_wins(self, session):
        _register("sa-wait-7", session)
        assert settle_run("sa-wait-7", {"status": "completed", "summary": "real"}) is True
        assert settle_run("sa-wait-7", {"status": "stalled", "summary": ""}) is False
        state = await_runs(_agent(session), ["sa-wait-7"], timeout_seconds=0)
        assert state["completed"][0]["status"] == "done"


# =========================================================================
# Yield claims (RunIdempotencyStore shape)
# =========================================================================

class TestYieldClaims:
    def test_created_reused_extended(self, session):
        outcome, _ = claim_yield(session, ["sa-c-1", "sa-c-2"])
        assert outcome == "created"
        _register("sa-c-1", session)
        _register("sa-c-2", session)
        outcome, _ = claim_yield(session, ["sa-c-2", "sa-c-1"])
        assert outcome == "reused"
        _register("sa-c-3", session)
        outcome, claim = claim_yield(session, ["sa-c-3"])
        assert outcome == "extended"
        assert claim["run_ids"] == ["sa-c-1", "sa-c-2", "sa-c-3"]
        assert pending_yielded_runs(session) == ["sa-c-1", "sa-c-2", "sa-c-3"]

    def test_inert_claim_is_treated_as_absent(self, session):
        _register("sa-c-4", session)
        claim_yield(session, ["sa-c-4"])
        settle_run("sa-c-4", {"status": "completed", "summary": "done"})
        assert pending_yielded_runs(session) == []
        outcome, _ = claim_yield(session, ["sa-c-4"])
        assert outcome == "created"


# =========================================================================
# sessions_yield (through the inline executor, like the live loop)
# =========================================================================

class TestSessionsYield:
    def test_nothing_pending_leaves_the_turn_alone(self, session):
        agent = _agent(session)
        result = _call("sessions_yield", agent, {})
        assert result["status"] == "nothing_pending"
        assert agent._yield_requested is None

    def test_yield_arms_the_turn_end(self, session):
        agent = _agent(session)
        _register("sa-y-1", session)
        _register("sa-y-2", session)
        result = _call("sessions_yield", agent, {"message": "pick up both summaries"})
        assert result["status"] == "yielded"
        assert result["runIds"] == ["sa-y-1", "sa-y-2"]
        assert result["acknowledgment"] == "Waiting for 2 subagents to finish — I'll continue when their results arrive."
        assert agent._yield_requested is not None
        assert agent._yield_requested["acknowledgment"] == result["acknowledgment"]

    def test_second_yield_reuses_the_claim(self, session):
        agent = _agent(session)
        _register("sa-y-3", session)
        _call("sessions_yield", agent, {})
        from tools.subagent_run_registry import consume_yield_request
        consume_yield_request(agent)  # the loop consumed the first yield
        result = _call("sessions_yield", agent, {})
        assert result["status"] == "already_pending"
        assert agent._yield_requested is None  # no new turn end armed

    def test_yield_after_settle_is_nothing_pending(self, session):
        agent = _agent(session)
        _register("sa-y-4", session)
        settle_run("sa-y-4", {"status": "completed", "summary": "done"})
        result = _call("sessions_yield", agent, {})
        assert result["status"] == "nothing_pending"

    def test_wait_for_message_yields_waiting_for_the_requester(self, session):
        # F4: waitFor "message" is resumable now — a main agent ends the
        # turn waiting (the child-park half lives in
        # test_turn_control_wait_for_message.py).
        agent = _agent(session)
        _register("sa-y-5", session)
        result = _call("sessions_yield", agent, {"waitFor": "message"})
        assert result["status"] == "yielded_waiting"
        assert agent._yield_requested is not None

    def test_bad_wait_for_is_rejected(self, session):
        agent = _agent(session)
        result = _call("sessions_yield", agent, {"waitFor": "forever"})
        assert "error" in result


# =========================================================================
# agents_wait (through the inline executor)
# =========================================================================

class TestAgentsWait:
    def test_collects_settled_and_reports_pending(self, session):
        _register("sa-aw-1", session)
        _register("sa-aw-2", session)
        settle_run("sa-aw-1", {"status": "completed", "summary": "A"})
        result = _call("agents_wait", _agent(session), {"ids": ["sa-aw-1", "sa-aw-2"], "timeoutSeconds": 0})
        assert [c["runId"] for c in result["completed"]] == ["sa-aw-1"]
        assert result["pending"] == ["sa-aw-2"]
        assert "errors" not in result

    def test_all_errors_reports_success_false(self, session):
        result = _call("agents_wait", _agent(session), {"ids": ["sa-nope"], "timeoutSeconds": 0})
        assert result["success"] is False
        assert result["errors"] == [{"runId": "sa-nope", "error": "not_found"}]

    def test_await_results_and_timeout_are_exclusive(self, session):
        result = _call("agents_wait", _agent(session), {"ids": ["sa-aw-3"], "timeoutSeconds": 5, "awaitResults": True})
        assert "error" in result

    def test_too_many_ids_rejected(self, session):
        result = _call("agents_wait", _agent(session), {"ids": [f"sa-x-{i}" for i in range(MAX_WAIT_IDS + 1)]})
        assert "error" in result and str(MAX_WAIT_IDS) in result["error"]

    def test_no_timeout_waits_for_settle(self, session):
        # Neither timeout nor awaitResults: the upstream 30s default applies,
        # and a settle still returns the call early (event-driven, not deadline).
        _register("sa-aw-4", session)
        threading.Timer(0.2, lambda: settle_run(
            "sa-aw-4", {"status": "completed", "summary": "late"})).start()
        started = time.monotonic()
        result = _call("agents_wait", _agent(session), {"ids": ["sa-aw-4"]})
        assert time.monotonic() - started < 5
        assert [c["runId"] for c in result["completed"]] == ["sa-aw-4"]

    def test_result_text_is_capped(self, session):
        _register("sa-aw-5", session)
        settle_run("sa-aw-5", {"status": "completed", "summary": "x" * 150_000})
        result = _call("agents_wait", _agent(session), {"ids": ["sa-aw-5"], "timeoutSeconds": 0})
        assert len(result["completed"][0]["result"]) == 100_000
        assert result["completed"][0]["resultTruncated"] is True


# =========================================================================
# Continuation message rides the completion event into the resumed turn
# =========================================================================

class TestYieldContinuationRendering:
    def _batch_event(self, message=None):
        evt = {
            "type": "async_delegation", "delegation_id": "d-yield-1", "is_batch": True,
            "goals": ["research A", "research B"],
            "results": [
                {"task_index": 0, "status": "completed", "summary": "A done"},
                {"task_index": 1, "status": "completed", "summary": "B done"},
            ],
            "status": "completed", "dispatched_at": time.time() - 60, "completed_at": time.time(),
            "total_duration_seconds": 60,
        }
        if message:
            evt["yield_message"] = message
        return evt

    def test_yield_message_renders_in_batch_completion(self):
        text = format_process_notification(self._batch_event("pick up both summaries"))
        assert "pick up both summaries" in text
        assert "A done" in text and "B done" in text

    def test_no_yield_message_adds_no_line(self):
        text = format_process_notification(self._batch_event())
        assert "yielded for these results" not in text
