"""sessions_yield(waitFor="message") (F4): child parks, main agent ends the turn.

A delegate child has no turn-resume machinery, so its wait parks inside
the tool call until sessions_send delivers; a main agent ends the turn
waiting and resumes as a new turn. Zero LLM calls.
"""

import json
import threading
import uuid
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from tools.subagent_run_registry import consume_yield_request, deliver_message


def _agent(session_id):
    return SimpleNamespace(
        session_id=session_id, _interrupt_requested=False, _yield_requested=None,
        _touch_activity=lambda _desc: None,
    )


def _ctx():
    return InlineToolContext(effective_task_id="t-1", tool_call_id="call-1")


def _call(name, agent, args):
    return json.loads(INLINE_TOOL_EXECUTORS[name](agent, args, _ctx()))


@pytest.fixture
def child_context(monkeypatch):
    from agent import delegation_context

    monkeypatch.setattr(delegation_context, "is_delegated_child_context", lambda: True)


@pytest.fixture
def main_context(monkeypatch):
    from agent import delegation_context

    monkeypatch.setattr(delegation_context, "is_delegated_child_context", lambda: False)


class TestChildPark:
    def test_delivered_message_is_the_tool_result(self, child_context):
        session_id = f"child-{uuid.uuid4().hex[:8]}"
        child = _agent(session_id)
        parked: dict = {}

        def _park():
            parked["result"] = _call("sessions_yield", child, {"waitFor": "message"})

        thread = threading.Thread(target=_park, daemon=True)
        thread.start()
        from tests.waiting import wait_until
        from tools import subagent_run_registry as run_registry

        assert wait_until(lambda: session_id in run_registry._message_waits, timeout=5)
        assert deliver_message(session_id, "go ahead") is True
        thread.join(timeout=5)
        assert parked["result"]["status"] == "message_received"
        assert parked["result"]["message"] == "go ahead"
        # The turn did NOT end: no yield was armed.
        assert child._yield_requested is None

    def test_interrupt_ends_the_park(self, child_context):
        session_id = f"child-{uuid.uuid4().hex[:8]}"
        child = _agent(session_id)
        parked: dict = {}

        def _park():
            parked["result"] = _call("sessions_yield", child, {"waitFor": "message"})

        thread = threading.Thread(target=_park, daemon=True)
        thread.start()
        from tests.waiting import wait_until
        from tools import subagent_run_registry as run_registry

        assert wait_until(lambda: session_id in run_registry._message_waits, timeout=5)
        child._interrupt_requested = True
        thread.join(timeout=5)
        assert "error" in parked["result"]


class TestMainAgentWait:
    def test_yield_arms_the_turn_end_and_claims_the_wait(self, main_context):
        session_id = f"s-test-{uuid.uuid4().hex[:8]}"
        agent = _agent(session_id)
        result = _call("sessions_yield", agent, {"waitFor": "message"})
        assert result["status"] == "yielded_waiting"
        assert agent._yield_requested is not None
        assert consume_yield_request(agent) == "Waiting for a message — I'll continue when it arrives."

    def test_second_yield_reuses_the_claim(self, main_context):
        session_id = f"s-test-{uuid.uuid4().hex[:8]}"
        agent = _agent(session_id)
        first = _call("sessions_yield", agent, {"waitFor": "message"})
        assert first["status"] == "yielded_waiting"
        assert consume_yield_request(agent) is not None
        second = _call("sessions_yield", agent, {"waitFor": "message"})
        assert second["status"] == "already_pending"
        assert agent._yield_requested is None

    def test_custom_acknowledgment_is_kept(self, main_context):
        session_id = f"s-test-{uuid.uuid4().hex[:8]}"
        agent = _agent(session_id)
        result = _call("sessions_yield", agent,
                       {"waitFor": "message", "acknowledgment": "On hold for your note."})
        assert result["acknowledgment"] == "On hold for your note."
        assert consume_yield_request(agent) == "On hold for your note."
