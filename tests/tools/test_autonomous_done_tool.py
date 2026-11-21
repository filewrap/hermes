"""Tests for the autonomous done channel (``tools/autonomous_done_tool.py``).

Behaviour contracts, all against the real run store in a temp launch home
and the real dispatch path (inline executor, like the F3 yield tests): the
tool refuses outside an autonomous turn, for a bad action, without a live
agent, for a session that is not the run's, and once the run already ended;
it transitions a running record to done / needs_input and arms the turn
end with the acknowledgment. Zero LLM calls.
"""

import json
from types import SimpleNamespace

import pytest

from agent.autonomous_context import is_autonomous_turn, set_autonomous_turn
from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from gateway import autonomous_state as store
from hermes_cli.config_defaults import DEFAULT_CONFIG
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_toolset
from tools.delegate_tool_toolsets import DELEGATE_BLOCKED_TOOLS
from tools.registry import registry

SESSION_KEY = "agent:main:telegram:dm:111"
GOAL = "Tidy the downloads folder"


@pytest.fixture
def launch_home(tmp_path, monkeypatch):
    """A fake launch home, installed as the routing process home."""
    home = tmp_path / "launch"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


@pytest.fixture
def autonomous_turn_fixture():
    set_autonomous_turn(True)
    yield
    set_autonomous_turn(False)


def _agent(session_key=SESSION_KEY):
    return SimpleNamespace(
        session_id="sess-1", gateway_session_key=session_key,
        _autonomous_done_requested=None)


def _ctx():
    return InlineToolContext(effective_task_id="t-1", tool_call_id="call-1")


def _call(agent, args):
    raw = INLINE_TOOL_EXECUTORS["autonomous_done"](agent, args, _ctx())
    return json.loads(raw)


def _start_run(home, session_key=SESSION_KEY, goal=GOAL):
    record = store.create_run(home, session_key, goal)
    store.start_run(home, record["run_id"])
    return record


def test_refuses_outside_an_autonomous_turn(launch_home):
    """No contextvar, no call — the record is untouched."""
    record = _start_run(launch_home)
    assert is_autonomous_turn() is False
    result = _call(_agent(), {"action": "done", "summary": "tidied"})
    assert "error" in result
    assert store.load_run(launch_home, record["run_id"])["state"] == "running"


def test_refuses_bad_action(launch_home, autonomous_turn_fixture):
    record = _start_run(launch_home)
    result = _call(_agent(), {"action": "finish"})
    assert "error" in result
    assert store.load_run(launch_home, record["run_id"])["state"] == "running"


def test_refuses_without_a_live_agent(launch_home, autonomous_turn_fixture):
    from tools.autonomous_done_tool import autonomous_done

    record = _start_run(launch_home)
    result = json.loads(autonomous_done(action="done", parent_agent=None))
    assert "error" in result
    assert store.load_run(launch_home, record["run_id"])["state"] == "running"


def test_refuses_a_session_that_is_not_the_run(launch_home, autonomous_turn_fixture):
    record = _start_run(launch_home)
    result = _call(_agent(session_key="agent:main:telegram:dm:222"),
                   {"action": "done"})
    assert "error" in result
    assert store.load_run(launch_home, record["run_id"])["state"] == "running"


def test_refuses_an_agent_without_a_session_key(launch_home, autonomous_turn_fixture):
    record = _start_run(launch_home)
    agent = SimpleNamespace(session_id="sess-1", _autonomous_done_requested=None)
    result = _call(agent, {"action": "done"})
    assert "error" in result
    assert store.load_run(launch_home, record["run_id"])["state"] == "running"


def test_refuses_when_no_run_is_live(launch_home, autonomous_turn_fixture):
    result = _call(_agent(), {"action": "done"})
    assert "error" in result


def test_done_transitions_the_record_and_arms_the_end(launch_home, autonomous_turn_fixture):
    record = _start_run(launch_home)
    agent = _agent()
    result = _call(agent, {"action": "done", "summary": "downloads tidied"})
    assert result == {
        "status": "done",
        "acknowledgment": "Done — downloads tidied",
    }
    assert store.load_run(launch_home, record["run_id"])["state"] == "done"
    assert agent._autonomous_done_requested == {
        "action": "done", "acknowledgment": "Done — downloads tidied",
    }


def test_done_without_a_summary_still_acks(launch_home, autonomous_turn_fixture):
    record = _start_run(launch_home)
    agent = _agent()
    result = _call(agent, {"action": "done"})
    assert result["acknowledgment"] == "Done."
    assert store.load_run(launch_home, record["run_id"])["state"] == "done"


def test_needs_input_pauses_the_run_and_arms_the_end(launch_home, autonomous_turn_fixture):
    record = _start_run(launch_home)
    agent = _agent()
    result = _call(agent, {"action": "needs_input", "summary": "which folder?"})
    assert result == {
        "status": "needs_input",
        "acknowledgment": "Waiting for your input: which folder?",
    }
    assert store.load_run(launch_home, record["run_id"])["state"] == "needs_input"
    assert agent._autonomous_done_requested["action"] == "needs_input"


def test_second_call_refuses_once_the_run_ended(launch_home, autonomous_turn_fixture):
    record = _start_run(launch_home)
    agent = _agent()
    assert _call(agent, {"action": "done"})["status"] == "done"
    agent._autonomous_done_requested = None
    result = _call(agent, {"action": "done"})
    assert "error" in result
    assert store.load_run(launch_home, record["run_id"])["state"] == "done"


def test_second_call_refuses_once_the_run_is_waiting(launch_home, autonomous_turn_fixture):
    record = _start_run(launch_home)
    agent = _agent()
    assert _call(agent, {"action": "needs_input"})["status"] == "needs_input"
    agent._autonomous_done_requested = None
    result = _call(agent, {"action": "done"})
    assert "error" in result
    assert store.load_run(launch_home, record["run_id"])["state"] == "needs_input"


class TestSurface:
    """The tool exists only inside autonomous turns: its own toolset (never
    core), deferred by default (never eager), and blocked for children."""

    def test_registered_in_the_autonomous_toolset(self):
        assert registry.get_tool_to_toolset_map().get("autonomous_done") == "autonomous"
        assert resolve_toolset("autonomous") == ["autonomous_done"]
        assert "autonomous" in TOOLSETS

    def test_never_in_core_tools(self):
        assert "autonomous_done" not in _HERMES_CORE_TOOLS

    def test_deferred_by_default(self):
        assert "autonomous_done" in DEFAULT_CONFIG["tools"]["tool_search"]["defer"]

    def test_blocked_for_delegated_children(self):
        assert "autonomous_done" in DELEGATE_BLOCKED_TOOLS
        assert all(tool in DELEGATE_BLOCKED_TOOLS
                   for tool in resolve_toolset("autonomous", include_registry=False))

    def test_invisible_to_ordinary_turns_visible_deferred_to_autonomous_ones(self):
        """Zero prompt bytes: an ordinary turn's tool defs never name the
        tool — not as a direct def, not in the bridge listing. Folding the
        autonomous toolset in (what begin() does for an autonomous turn)
        still adds no direct def — the tool rides the bridge, named in its
        listing, loadable on demand."""
        import model_tools

        def _names(defs):
            return [(d.get("function") or {}).get("name") for d in defs]

        def _bridge(defs):
            return next(d for d in defs if _names([d])[0] == "tool_search")

        ordinary = model_tools.get_tool_definitions(
            enabled_toolsets=["hermes-telegram"], quiet_mode=True)
        assert "autonomous_done" not in _names(ordinary)
        assert "autonomous_done" not in _bridge(ordinary)["function"]["description"]

        autonomous = model_tools.get_tool_definitions(
            enabled_toolsets=["hermes-telegram", "autonomous"], quiet_mode=True)
        assert "autonomous_done" not in _names(autonomous)
        assert "autonomous_done" in _bridge(autonomous)["function"]["description"]
