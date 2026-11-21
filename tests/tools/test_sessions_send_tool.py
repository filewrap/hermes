"""F4 sessions_send: child runs by delivery/steer, gateway sessions by injection.

Live-check shape, in the sandbox: a parked child yield is resumed by the
delivered message; a running child takes the message as a steer; a gateway
session receives it through the (stubbed) injection dispatch. Zero LLM
calls; no live gateway, no live Telegram connection.
"""

import json
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from tests.waiting import wait_until
from tools import subagent_run_registry as run_registry
from tools.delegate_tool_registry import _register_subagent, _unregister_subagent
from tools.subagent_run_registry import (
    claim_session_wait, register_run, settle_run,
)
from tools.sessions_send_tool import sessions_send


@pytest.fixture
def session():
    return f"s-test-{uuid.uuid4().hex[:8]}"


def _agent(session_id):
    return SimpleNamespace(
        session_id=session_id, _interrupt_requested=False, _yield_requested=None,
        _touch_activity=lambda _desc: None,
    )


def _ctx():
    return InlineToolContext(effective_task_id="t-1", tool_call_id="call-1")


def _call(name, agent, args):
    return json.loads(INLINE_TOOL_EXECUTORS[name](agent, args, _ctx()))


def _register_child(run_id, parent_session, child_session):
    return register_run(run_id, goal=f"task {run_id}",
                        owner_agent_session_id=parent_session,
                        child_session_id=child_session)


class TestSendToChild:
    def test_parked_child_resumes_with_the_message(self, session, monkeypatch):
        from agent import delegation_context

        monkeypatch.setattr(delegation_context, "is_delegated_child_context", lambda: True)
        child_session = f"child-{uuid.uuid4().hex[:8]}"
        _register_child("run-parked", session, child_session)
        child = _agent(child_session)
        parked_result: dict = {}

        def _park():
            parked_result["result"] = _call(
                "sessions_yield", child, {"waitFor": "message"})

        thread = threading.Thread(target=_park, daemon=True)
        thread.start()
        assert wait_until(
            lambda: child_session in run_registry._message_waits, timeout=5)
        result = _call("sessions_send", _agent(session),
                       {"sessionKey": "run-parked", "message": "go ahead"})
        thread.join(timeout=5)
        assert result["success"] is True
        assert result["status"] == "delivered"
        assert result["delivery"] == "waiting_subagent"
        assert parked_result["result"]["status"] == "message_received"
        assert parked_result["result"]["message"] == "go ahead"

    def test_running_child_is_steered(self, session):
        received: list[str] = []

        class _FakeChild:
            def steer(self, text):
                received.append(text)
                return True

        _register_child("run-steer", session, f"child-{uuid.uuid4().hex[:8]}")
        _register_subagent({
            "subagent_id": "run-steer", "agent": _FakeChild(),
            "owner_agent_session_id": session, "goal": "task",
        })
        try:
            result = _call("sessions_send", _agent(session),
                           {"sessionKey": "run-steer", "message": "change of plan"})
        finally:
            _unregister_subagent("run-steer")
        assert result["success"] is True
        assert result["status"] == "delivered"
        assert result["delivery"] == "steered"
        assert received == ["change of plan"]

    def test_finished_run_is_refused(self, session):
        _register_child("run-done", session, f"child-{uuid.uuid4().hex[:8]}")
        settle_run("run-done", {"status": "completed", "summary": "done"})
        result = _call("sessions_send", _agent(session),
                       {"sessionKey": "run-done", "message": "too late"})
        assert result["success"] is False
        assert "finished" in result["error"]

    def test_another_sessions_run_is_not_mine(self, session):
        _register_child("run-foreign", "someone-else", f"child-{uuid.uuid4().hex[:8]}")
        result = _call("sessions_send", _agent(session),
                       {"sessionKey": "run-foreign", "message": "hello"})
        assert result["success"] is False
        assert "another session" in result["error"]


class TestSendToSession:
    @pytest.fixture
    def fake_db(self):
        return SimpleNamespace(
            list_sessions_rich=lambda **_kw: [],
            get_session=lambda _sid: {},
            resolve_resume_session_id=lambda sid: sid,
        )

    @pytest.fixture
    def fake_runner(self, monkeypatch):
        import gateway.run as gateway_run

        runner = SimpleNamespace(
            session_store=SimpleNamespace(
                lookup_by_session_id=lambda sid: SimpleNamespace(
                    session_key=f"agent:main:telegram:{sid}", session_id=sid)
                if sid.startswith("sess-") else None),
            _gateway_loop=None,
            _dispatch_plugin_message_injection=AsyncMock(return_value=True),
        )
        monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
        return runner

    def test_session_turn_delivery(self, session, fake_db, fake_runner):
        result = json.loads(sessions_send(
            session_key="sess-1", message="pick this up",
            db=fake_db, current_session_id=session, parent_agent=_agent(session)))
        assert result["success"] is True
        assert result["status"] == "delivered"
        assert result["delivery"] == "session_turn"
        assert result["session_id"] == "sess-1"
        assert result["was_waiting"] is False
        call = fake_runner._dispatch_plugin_message_injection.await_args
        assert call.kwargs["session_key"] == "agent:main:telegram:sess-1"
        assert call.kwargs["content"] == "pick this up"

    def test_waiting_session_reports_it_was_waiting(self, session, fake_db, fake_runner):
        claim_session_wait("sess-2", message="waiting for this")
        result = json.loads(sessions_send(
            session_key="sess-2", message="here it is",
            db=fake_db, current_session_id=session, parent_agent=_agent(session)))
        assert result["success"] is True
        assert result["was_waiting"] is True

    def test_unknown_session_is_refused(self, session, fake_db, fake_runner):
        result = json.loads(sessions_send(
            session_key="nope", message="hello",
            db=fake_db, current_session_id=session, parent_agent=_agent(session)))
        assert result["success"] is False

    def test_own_session_is_refused(self, session, fake_db, fake_runner):
        result = json.loads(sessions_send(
            session_key=session, message="hello",
            db=fake_db, current_session_id=session, parent_agent=_agent(session)))
        assert result["success"] is False
        assert "own session" in result["error"]

    def test_no_live_gateway_fails_closed(self, session, fake_db, monkeypatch):
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: None)
        result = json.loads(sessions_send(
            session_key="sess-9", message="hello",
            db=fake_db, current_session_id=session, parent_agent=_agent(session)))
        assert result["success"] is False
        assert "No live gateway" in result["error"]

    def test_empty_message_fails_closed(self, session):
        result = _call("sessions_send", _agent(session),
                       {"sessionKey": "sess-1", "message": "   "})
        assert result["success"] is False
