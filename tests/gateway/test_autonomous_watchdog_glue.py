"""Behaviour contracts for the watchdog glue (``gateway/autonomous_turn.py``).

The checkpoint wrapper still checkpoints and forwards to the previous
callback after feeding the watchdog; a stuck episode records the agent
note once, bumps the run's stall count, and hops the owner status +
throttled pinned update onto the gateway loop — without halting the run.
All against the real run store in a temp launch home.
"""

import threading
from types import SimpleNamespace

import pytest

from gateway import autonomous_state as store
from gateway import autonomous_turn
from gateway.run_turn_runner import TurnRunner

SESSION_KEY = "agent:main:telegram:dm:111"
CHAT_ID = "111"
GOAL = "Tidy the downloads folder"


@pytest.fixture
def launch_home(tmp_path, monkeypatch):
    """A fake launch home, installed as the routing process home."""
    home = tmp_path / "launch"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


class _FakeAdapter:
    """Records direct MTProto sends and pin edits."""

    def __init__(self):
        self.sends = []
        self.edits = []

    async def send(self, *, chat_id, content, reply_to=None, metadata=None):
        self.sends.append({"chat_id": str(chat_id), "content": content})
        return SimpleNamespace(success=True)

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.edits.append({
            "chat_id": str(chat_id), "message_id": str(message_id),
            "content": content})
        return SimpleNamespace(success=True)


def _agent(**over):
    agent = SimpleNamespace(
        model="deepseek-v4", provider="amd", session_prompt_tokens=0,
        session_completion_tokens=0, run_budget_seconds=None,
        tool_complete_callback=None)
    for key, value in over.items():
        setattr(agent, key, value)
    return agent


def _turn_runner(runner_ns, adapter, agent=None, *, with_loop=True):
    turn_runner = object.__new__(TurnRunner)
    turn_runner._runner = runner_ns
    ctx = SimpleNamespace(
        session_key=SESSION_KEY, message="do the thing",
        agent_holder=[agent if agent is not None else _agent()],
        source=SimpleNamespace(chat_id=CHAT_ID))
    if with_loop:
        ctx._loop_for_step = object()
    turn_runner._ctx = ctx
    scheduled = []
    turn_runner._schedule = lambda coro, log_message, loop=None: scheduled.append(
        (coro, log_message))
    turn_runner._scheduled = scheduled
    return turn_runner


def _runner_ns(adapter):
    return SimpleNamespace(
        _agent_cache={}, _agent_cache_lock=threading.Lock(),
        _autonomous_watchdogs={},
        _pending_autonomous_watchdog_notes={},
        _delivery_adapter_for=lambda source: adapter)


def _start_run(home, *, pin=True):
    record = store.create_run(home, SESSION_KEY, GOAL)
    store.start_run(home, record["run_id"])
    if pin:
        store.set_active(home, True, run_id=record["run_id"],
                         pinned_message_id="777", chat_id=CHAT_ID)
    return record


def _begin(launch_home, adapter, agent=None, *, with_loop=True):
    runner_ns = _runner_ns(adapter)
    turn_runner = _turn_runner(runner_ns, adapter, agent, with_loop=with_loop)
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is True
    return turn_runner


def _drive(agent, calls):
    for i, (tool, args, result) in enumerate(calls):
        agent.tool_complete_callback(f"c{i}", tool, args, result)


def _identical(n=3):
    return [("read_file", {"path": "/tmp/x"}, "contents")] * n


@pytest.mark.asyncio
async def test_wrapper_still_checkpoints_and_forwards(launch_home):
    adapter = _FakeAdapter()
    agent = _agent()
    forwarded = []
    agent.tool_complete_callback = lambda call_id, name, args, result: forwarded.append(
        (call_id, name, args, result))
    record = _start_run(launch_home)
    turn_runner = _begin(launch_home, adapter, agent)
    autonomous_turn.install_tool_checkpoint(turn_runner, agent)

    _drive(agent, [("read_file", {"path": "/tmp/x"}, "contents")])
    _drive(agent, [("terminal", {"command": "ls"}, "a\nb")])
    # Unusual inputs must not break the wrapper or the forward.
    agent.tool_complete_callback("cx", "tool", object(), object())

    assert len(forwarded) == 3
    assert forwarded[0] == ("c0", "read_file", {"path": "/tmp/x"}, "contents")
    assert forwarded[1][1] == "terminal"
    assert store.load_run(launch_home, record["run_id"])["steps"] == 3
    assert turn_runner._scheduled == []


@pytest.mark.asyncio
async def test_stuck_episode_records_note_once_and_bumps_stall_count(launch_home):
    adapter = _FakeAdapter()
    agent = _agent()
    record = _start_run(launch_home)
    turn_runner = _begin(launch_home, adapter, agent)
    autonomous_turn.install_tool_checkpoint(turn_runner, agent)

    _drive(agent, _identical(5))

    notes = turn_runner._runner._pending_autonomous_watchdog_notes
    assert list(notes) == [SESSION_KEY]
    assert "read_file" in notes[SESSION_KEY]
    assert "identical arguments" in notes[SESSION_KEY]
    assert "Change strategy, use what you have, or declare done." in notes[SESSION_KEY]
    stored = store.load_run(launch_home, record["run_id"])
    assert stored["stall_count"] == 1
    assert stored["steps"] == 5


@pytest.mark.asyncio
async def test_note_prepends_into_next_turn_message_once(launch_home):
    adapter = _FakeAdapter()
    agent = _agent()
    _start_run(launch_home)
    turn_runner = _begin(launch_home, adapter, agent)
    autonomous_turn.install_tool_checkpoint(turn_runner, agent)
    _drive(agent, _identical(3))
    assert SESSION_KEY in turn_runner._runner._pending_autonomous_watchdog_notes

    stub = SimpleNamespace(
        _runner=turn_runner._runner,
        _ctx=SimpleNamespace(session_key=SESSION_KEY, message="do the thing"))
    TurnRunner._prepend_pending_note(stub, "_pending_autonomous_watchdog_notes")

    assert stub._ctx.message.startswith("[Autonomous watchdog note:")
    assert stub._ctx.message.endswith("do the thing")
    assert SESSION_KEY not in turn_runner._runner._pending_autonomous_watchdog_notes
    TurnRunner._prepend_pending_note(stub, "_pending_autonomous_watchdog_notes")
    assert stub._ctx.message.count("[Autonomous watchdog note:") == 1


@pytest.mark.asyncio
async def test_owner_notice_and_pin_sent_with_goal_steps_tokens(launch_home):
    adapter = _FakeAdapter()
    agent = _agent()
    record = _start_run(launch_home)
    store.checkpoint(launch_home, record["run_id"],
                     input_tokens=1200, output_tokens=300)
    turn_runner = _begin(launch_home, adapter, agent)
    autonomous_turn.install_tool_checkpoint(turn_runner, agent)

    _drive(agent, _identical(3))
    for coro, _log in turn_runner._scheduled:
        await coro

    assert len(adapter.sends) == 1
    notice = adapter.sends[0]["content"]
    assert adapter.sends[0]["chat_id"] == CHAT_ID
    assert GOAL in notice and "read_file" in notice
    assert "3" in notice and "1500" in notice
    assert "/stop" in notice
    assert len(adapter.edits) == 1
    pin = adapter.edits[0]
    assert pin["message_id"] == "777" and pin["chat_id"] == CHAT_ID
    assert GOAL in pin["content"] and "1500" in pin["content"]
    assert "read_file" in pin["content"] and "/stop" in pin["content"]


@pytest.mark.asyncio
async def test_pin_update_throttled_within_window(launch_home):
    adapter = _FakeAdapter()
    agent = _agent()
    _start_run(launch_home)
    turn_runner = _begin(launch_home, adapter, agent)
    autonomous_turn.install_tool_checkpoint(turn_runner, agent)

    _drive(agent, _identical(3))  # episode 1 at step 3: notice + pin edit
    _drive(agent, [("terminal", {"command": "ls"}, "a\nb")])  # progress ends it
    _drive(agent, _identical(3))  # episode 2 at step 7: notice only, pin throttled
    for coro, _log in turn_runner._scheduled:
        await coro
    done = len(turn_runner._scheduled)

    assert len(adapter.sends) == 2
    assert len(adapter.edits) == 1
    # Past the 10-step gate the next episode edits the pin again.
    _drive(agent, [("terminal", {"command": "pwd"}, "/home")])
    _drive(agent, _identical(3))  # episode 3 at step 11: still throttled (11-3 < 10)
    _drive(agent, [("terminal", {"command": "whoami"}, "kei")])
    _drive(agent, _identical(3))  # episode 4 at step 15: pin edit allowed
    for coro, _log in turn_runner._scheduled[done:]:
        await coro

    assert len(adapter.sends) == 4
    assert len(adapter.edits) == 2


@pytest.mark.asyncio
async def test_no_sends_without_loop_but_note_still_recorded(launch_home):
    adapter = _FakeAdapter()
    agent = _agent()
    _start_run(launch_home)
    turn_runner = _begin(launch_home, adapter, agent, with_loop=False)
    autonomous_turn.install_tool_checkpoint(turn_runner, agent)

    _drive(agent, _identical(3))

    assert SESSION_KEY in turn_runner._runner._pending_autonomous_watchdog_notes
    assert turn_runner._scheduled == []
    assert adapter.sends == [] and adapter.edits == []


def test_inactive_turn_observes_nothing(launch_home):
    adapter = _FakeAdapter()
    agent = _agent()
    turn_runner = _turn_runner(_runner_ns(adapter), adapter, agent)

    autonomous_turn.install_tool_checkpoint(turn_runner, agent)
    autonomous_turn.observe_watchdog(turn_runner, "read_file", {"path": "/x"}, "c")

    assert agent.tool_complete_callback is None
    assert getattr(turn_runner._runner, "_pending_autonomous_watchdog_notes", {}) == {}
