"""Turn wiring for autonomous mode (``gateway/autonomous_turn.py``).

Behaviour contracts, all against the real run store in a temp launch home:
the live flag is read at turn start; a cached agent built under the other
flag is evicted on a flip; the run record gets the turn's resolved model;
the run-start line is prepended once per run as ordinary conversation
content; arming a turn sets the contextvar + the approval session set and
nulls the run budget (restored afterwards); every completed tool call
checkpoints the run ledger.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from agent.autonomous_context import is_autonomous_turn, set_autonomous_turn
from gateway import autonomous_state as store
from gateway import autonomous_turn
from gateway.run_turn_runner import TurnRunner
from tools import approval

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
def autonomous_off(monkeypatch):
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    set_autonomous_turn(False)
    yield
    set_autonomous_turn(False)
    approval.clear_session(SESSION_KEY)


def _agent(**over):
    agent = SimpleNamespace(
        model="deepseek-v4", provider="amd", session_prompt_tokens=0,
        session_completion_tokens=0, run_budget_seconds=None,
        tool_complete_callback=None)
    for key, value in over.items():
        setattr(agent, key, value)
    return agent


def _turn_runner(runner_ns, session_key=SESSION_KEY, message="do the thing",
                 agent=None):
    turn_runner = object.__new__(TurnRunner)
    turn_runner._runner = runner_ns
    turn_runner._ctx = SimpleNamespace(
        session_key=session_key, message=message,
        agent_holder=[agent if agent is not None else _agent()])
    return turn_runner


def _runner_ns():
    return SimpleNamespace(
        _agent_cache={}, _agent_cache_lock=threading.Lock())


def _start_run(home, session_key=SESSION_KEY, goal=GOAL):
    record = store.create_run(home, session_key, goal)
    store.start_run(home, record["run_id"])
    return record


def test_begin_inactive_when_no_run(launch_home, autonomous_off):
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is False
    assert turn_runner._autonomous_run_id is None
    assert is_autonomous_turn() is False


def test_begin_active_for_matching_session(launch_home, autonomous_off):
    record = _start_run(launch_home)
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is True
    assert turn_runner._autonomous_run_id == record["run_id"]
    assert turn_runner._autonomous_home == launch_home


def test_begin_ignores_run_of_another_session(launch_home, autonomous_off):
    _start_run(launch_home)
    turn_runner = _turn_runner(_runner_ns(), session_key="agent:main:telegram:dm:222")
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is False
    assert turn_runner._autonomous_run_id is None


def test_begin_fails_closed_when_record_is_missing(launch_home, autonomous_off):
    record = _start_run(launch_home)
    (launch_home / "autonomous" / "runs" / f"{record['run_id']}.json").unlink()
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is False


def test_begin_resets_stale_per_turn_state(launch_home, autonomous_off):
    turn_runner = _turn_runner(_runner_ns())
    turn_runner._autonomous_active = True
    turn_runner._autonomous_run_id = "run_stale"
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is False
    assert turn_runner._autonomous_run_id is None


def test_begin_evicts_cached_agent_on_flip(launch_home, autonomous_off):
    """An ordinary-built cached agent does not survive the flag turning on."""
    record = _start_run(launch_home)
    runner_ns = _runner_ns()
    cached = _agent()
    runner_ns._agent_cache[SESSION_KEY] = (cached, "sig", 0, "sid-1")
    released = []
    turn_runner = _turn_runner(runner_ns)
    turn_runner._release_evicted_agent = lambda agent: released.append(agent)
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is True
    assert SESSION_KEY not in runner_ns._agent_cache
    assert released == [cached]
    assert record["run_id"] == turn_runner._autonomous_run_id


def test_begin_keeps_cached_agent_built_under_autonomous(launch_home, autonomous_off):
    """No flip, no eviction: the prompt cache survives autonomous turns."""
    _start_run(launch_home)
    runner_ns = _runner_ns()
    cached = _agent(_autonomous_at_build=True)
    runner_ns._agent_cache[SESSION_KEY] = (cached, "sig", 0, "sid-1")
    released = []
    turn_runner = _turn_runner(runner_ns)
    turn_runner._release_evicted_agent = lambda agent: released.append(agent)
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is True
    assert runner_ns._agent_cache[SESSION_KEY][0] is cached
    assert released == []


def test_begin_evicts_autonomous_built_agent_when_flag_is_off(launch_home, autonomous_off):
    """A flip in the other direction rebuilds too."""
    runner_ns = _runner_ns()
    cached = _agent(_autonomous_at_build=True)
    runner_ns._agent_cache[SESSION_KEY] = (cached, "sig", 0, "sid-1")
    released = []
    turn_runner = _turn_runner(runner_ns)
    turn_runner._release_evicted_agent = lambda agent: released.append(agent)
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is False
    assert SESSION_KEY not in runner_ns._agent_cache
    assert released == [cached]


def test_stamp_run_model_records_resolved_model(launch_home, autonomous_off):
    record = _start_run(launch_home)
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    autonomous_turn.stamp_run_model(turn_runner, _agent())
    stamped = store.load_run(launch_home, record["run_id"])
    assert stamped["model"] == {"model": "deepseek-v4", "provider": "amd"}


def test_start_message_prepended_once_per_run(launch_home, autonomous_off):
    _start_run(launch_home)
    runner_ns = _runner_ns()
    first = _turn_runner(runner_ns)
    autonomous_turn.begin(first)
    autonomous_turn.maybe_prepend_start_message(first)
    assert first._ctx.message.startswith(
        "Autonomous mode is on: keep working until the goal is done.")
    assert f"Goal: {GOAL}" in first._ctx.message
    assert first._ctx.message.endswith("do the thing")

    # A second turn of the same run does not repeat the line.
    second = _turn_runner(runner_ns)
    autonomous_turn.begin(second)
    autonomous_turn.maybe_prepend_start_message(second)
    assert second._ctx.message == "do the thing"


def test_start_message_skipped_for_empty_message(launch_home, autonomous_off):
    _start_run(launch_home)
    turn_runner = _turn_runner(_runner_ns(), message="   ")
    autonomous_turn.begin(turn_runner)
    autonomous_turn.maybe_prepend_start_message(turn_runner)
    assert turn_runner._ctx.message == "   "


def test_start_message_skipped_when_not_autonomous(launch_home, autonomous_off):
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    autonomous_turn.maybe_prepend_start_message(turn_runner)
    assert turn_runner._ctx.message == "do the thing"


def test_stop_message_prepended_once_after_disable(launch_home, autonomous_off):
    record = _start_run(launch_home)
    # The run dies mid-turn; the owner disables. The record stays stopped.
    store.disable(launch_home, stopped_by="autonomous_disable")
    assert store.load_run(launch_home, record["run_id"])["state"] == "stopped"

    # Another session never sees the line — and the note stays pending.
    other = _turn_runner(_runner_ns(), session_key="agent:main:telegram:dm:222")
    autonomous_turn.maybe_prepend_stop_message(other)
    assert other._ctx.message == "do the thing"

    # The next message in the run's own DM: an ordinary turn whose message
    # carries the run-closed line naming the stopped goal.
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is False
    autonomous_turn.maybe_prepend_stop_message(turn_runner)
    assert turn_runner._ctx.message.startswith(
        "The previous autonomous run was stopped by the owner.")
    assert "Its task is closed" in turn_runner._ctx.message
    assert f"Goal: {GOAL}" in turn_runner._ctx.message
    assert turn_runner._ctx.message.endswith("do the thing")

    # Consumed once: the following turn gets no line.
    following = _turn_runner(_runner_ns())
    autonomous_turn.begin(following)
    autonomous_turn.maybe_prepend_stop_message(following)
    assert following._ctx.message == "do the thing"


def test_stop_message_arms_for_failed_and_waiting_runs_and_resume_drops_it(
        launch_home, autonomous_off):
    # A run that died mid-turn (failed) still arms the note on disable.
    failed = _start_run(launch_home)
    store.mark_failed(launch_home, failed["run_id"], last_error="boom")
    store.disable(launch_home, stopped_by="autonomous_disable")
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.maybe_prepend_stop_message(turn_runner)
    assert "Its task is closed" in turn_runner._ctx.message

    # A waiting run arms it too.
    waiting = _start_run(launch_home)
    store.mark_needs_input(launch_home, waiting["run_id"])
    store.disable(launch_home, stopped_by="autonomous_disable")
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.maybe_prepend_stop_message(turn_runner)
    assert "Its task is closed" in turn_runner._ctx.message

    # An explicit Resume drops the note unseen: the resumed turn gets no
    # closed line for the run it is continuing.
    resumable = _start_run(launch_home)
    store.mark_stopped(launch_home, resumable["run_id"])
    store.disable(launch_home, stopped_by="autonomous_disable")
    store.reopen_run(launch_home, resumable["run_id"])
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    autonomous_turn.maybe_prepend_stop_message(turn_runner)
    assert turn_runner._ctx.message == "do the thing"


def test_arm_disarm_bypass_and_budget(launch_home, autonomous_off):
    """Arming sets the flag, the session set and a null budget; disarming restores all three."""
    _start_run(launch_home)
    agent = _agent(run_budget_seconds=120)
    turn_runner = _turn_runner(_runner_ns(), agent=agent)
    autonomous_turn.begin(turn_runner)

    prev_budget = autonomous_turn.arm_conversation(turn_runner, agent, SESSION_KEY)
    try:
        assert prev_budget == 120
        assert is_autonomous_turn() is True
        assert approval.is_session_autonomous_enabled(SESSION_KEY) is True
        assert approval.is_session_yolo_enabled(SESSION_KEY) is False
        assert agent.run_budget_seconds is None
    finally:
        autonomous_turn.disarm_conversation(turn_runner, agent, SESSION_KEY, prev_budget)
    assert is_autonomous_turn() is False
    assert approval.is_session_autonomous_enabled(SESSION_KEY) is False
    assert agent.run_budget_seconds == 120


def test_arm_is_noop_when_not_autonomous(launch_home, autonomous_off):
    agent = _agent(run_budget_seconds=120)
    turn_runner = _turn_runner(_runner_ns(), agent=agent)
    autonomous_turn.begin(turn_runner)
    assert autonomous_turn.arm_conversation(turn_runner, agent, SESSION_KEY) is None
    assert is_autonomous_turn() is False
    assert approval.is_session_autonomous_enabled(SESSION_KEY) is False
    assert agent.run_budget_seconds == 120
    autonomous_turn.disarm_conversation(turn_runner, agent, SESSION_KEY, None)
    assert agent.run_budget_seconds == 120


def test_tool_checkpoint_after_each_call(launch_home, autonomous_off):
    """Each completed tool call checkpoints steps, last tool and run token totals."""
    record = _start_run(launch_home)
    agent = _agent()
    turn_runner = _turn_runner(_runner_ns(), agent=agent)
    autonomous_turn.begin(turn_runner)
    autonomous_turn.arm_conversation(turn_runner, agent, SESSION_KEY)

    forwarded = []
    agent.tool_complete_callback = lambda c, n, a, r: forwarded.append(n)
    autonomous_turn.install_tool_checkpoint(turn_runner, agent)

    agent.session_prompt_tokens, agent.session_completion_tokens = 100, 50
    agent.tool_complete_callback("c1", "terminal", {}, "ok")
    agent.session_prompt_tokens, agent.session_completion_tokens = 250, 90
    agent.tool_complete_callback("c2", "read_file", {}, "ok")

    assert forwarded == ["terminal", "read_file"]
    saved = store.load_run(launch_home, record["run_id"])
    assert saved["steps"] == 2
    assert saved["last_tool"] == "read_file"
    assert saved["input_tokens"] == 250
    assert saved["output_tokens"] == 90
    autonomous_turn.disarm_conversation(turn_runner, agent, SESSION_KEY, None)


def test_tool_checkpoint_without_arm_counts_steps_only(launch_home, autonomous_off):
    """Without token baselines a checkpoint still records the step, never raises."""
    record = _start_run(launch_home)
    agent = _agent(session_prompt_tokens=100, session_completion_tokens=50)
    turn_runner = _turn_runner(_runner_ns(), agent=agent)
    autonomous_turn.begin(turn_runner)
    autonomous_turn.install_tool_checkpoint(turn_runner, agent)
    agent.tool_complete_callback("c1", "terminal", {}, "ok")
    saved = store.load_run(launch_home, record["run_id"])
    assert saved["steps"] == 1
    assert saved["last_tool"] == "terminal"
    assert saved["input_tokens"] == 0
    assert saved["output_tokens"] == 0


def test_install_checkpoint_is_noop_when_not_autonomous(launch_home, autonomous_off):
    agent = _agent()
    turn_runner = _turn_runner(_runner_ns(), agent=agent)
    autonomous_turn.begin(turn_runner)
    autonomous_turn.install_tool_checkpoint(turn_runner, agent)
    assert agent.tool_complete_callback is None


def test_begin_resumes_a_waiting_run(launch_home, autonomous_off):
    """needs_input → running on the owner's next message in this session."""
    record = _start_run(launch_home)
    store.mark_needs_input(launch_home, record["run_id"])
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is True
    assert store.is_active(launch_home) is True
    assert store.load_run(launch_home, record["run_id"])["state"] == "running"


def test_begin_clears_a_stale_flag_for_a_done_run(launch_home, autonomous_off):
    """A done record under a live flag (a crash between the end and the
    flag clear) runs ordinary, and the stale flag is cleared."""
    record = _start_run(launch_home)
    store.mark_done(launch_home, record["run_id"])
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is False
    assert store.is_active(launch_home) is False
    assert store.load_run(launch_home, record["run_id"])["state"] == "done"


def test_begin_keeps_flag_for_a_stopped_run(launch_home, autonomous_off):
    """A stopped run under a live flag keeps the mode on: the flag is the
    owner's switch, not the run's. The next turn arms the approval bypass
    (see test_arm_arms_bypass_only_for_failed_run), and the run-closed
    note arms so the agent does not continue the dead task from history."""
    record = _start_run(launch_home)
    store.mark_stopped(launch_home, record["run_id"], stopped_by="stop_command")
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is False  # no live run
    assert store.is_active(launch_home) is True  # mode still on
    assert store.load_run(launch_home, record["run_id"])["state"] == "stopped"
    autonomous_turn.maybe_prepend_stop_message(turn_runner)
    assert "Its task is closed" in turn_runner._ctx.message


def test_begin_keeps_flag_for_a_failed_run(launch_home, autonomous_off):
    """Same as stopped: a failed run never silently disables the mode."""
    record = _start_run(launch_home)
    store.mark_failed(launch_home, record["run_id"], last_error="boom")
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is False  # no live run
    assert store.is_active(launch_home) is True  # mode still on
    assert store.load_run(launch_home, record["run_id"])["state"] == "failed"


def test_arm_arms_bypass_only_for_failed_run(launch_home, autonomous_off):
    """Mode on + failed run: the approval bypass arms (no card), but the
    run machinery stays off — the budget is NOT nulled, no run budget
    check is stamped, no checkpoints ride the turn."""
    record = _start_run(launch_home)
    store.mark_failed(launch_home, record["run_id"], last_error="boom")
    agent = _agent(run_budget_seconds=120)
    turn_runner = _turn_runner(_runner_ns(), agent=agent)
    autonomous_turn.begin(turn_runner)
    try:
        prev_budget = autonomous_turn.arm_conversation(turn_runner, agent, SESSION_KEY)
        assert prev_budget is None
        assert is_autonomous_turn() is True
        assert approval.is_session_autonomous_enabled(SESSION_KEY) is True
        assert approval.is_approval_bypass_active_for_session(SESSION_KEY) is True
        assert agent.run_budget_seconds == 120  # run machinery stayed off
    finally:
        autonomous_turn.disarm_conversation(turn_runner, agent, SESSION_KEY, prev_budget)
    assert is_autonomous_turn() is False
    assert approval.is_session_autonomous_enabled(SESSION_KEY) is False
    assert agent.run_budget_seconds == 120


def test_begin_unpins_a_stale_pin_for_a_done_run(launch_home, autonomous_off):
    record = _start_run(launch_home)
    store.set_active(launch_home, True, run_id=record["run_id"],
                     pinned_message_id="123", chat_id="111")
    store.mark_done(launch_home, record["run_id"])
    scheduled = []
    runner_ns = _runner_ns()
    runner_ns._delivery_adapter_for = lambda source: SimpleNamespace()
    turn_runner = _turn_runner(runner_ns)
    turn_runner._ctx.source = SimpleNamespace(chat_id="111")
    turn_runner._ctx._loop_for_step = object()
    turn_runner._schedule = lambda coro, msg: scheduled.append((coro, msg))
    autonomous_turn.begin(turn_runner)
    assert turn_runner._autonomous_active is False
    assert store.is_active(launch_home) is False
    assert len(scheduled) == 1
    assert scheduled[0][1] == "Autonomous pinned unpin"


def test_begin_folds_in_the_autonomous_toolset(launch_home, autonomous_off):
    """The done channel is visible only in autonomous turns: the toolset
    joins the turn's toolsets when the flag is on, and never otherwise."""
    _start_run(launch_home)
    turn_runner = _turn_runner(_runner_ns())
    turn_runner._ctx.enabled_toolsets = ["terminal"]
    autonomous_turn.begin(turn_runner)
    assert turn_runner._ctx.enabled_toolsets == ["terminal", "autonomous"]

    # A second begin does not duplicate it.
    autonomous_turn.begin(turn_runner)
    assert turn_runner._ctx.enabled_toolsets == ["terminal", "autonomous"]


def test_begin_does_not_fold_in_the_toolset_when_ordinary(launch_home, autonomous_off):
    turn_runner = _turn_runner(_runner_ns())
    turn_runner._ctx.enabled_toolsets = ["terminal"]
    autonomous_turn.begin(turn_runner)
    assert turn_runner._ctx.enabled_toolsets == ["terminal"]


def test_start_message_names_the_done_tool(launch_home, autonomous_off):
    _start_run(launch_home)
    turn_runner = _turn_runner(_runner_ns())
    autonomous_turn.begin(turn_runner)
    autonomous_turn.maybe_prepend_start_message(turn_runner)
    assert "autonomous_done" in turn_runner._ctx.message


def _finish_harness(launch_home):
    """A turn runner wired to capture gateway-loop sends."""
    record = _start_run(launch_home)
    store.set_active(launch_home, True, run_id=record["run_id"],
                     pinned_message_id="123", chat_id="111")
    agent = _agent()
    runner_ns = _runner_ns()
    calls = []

    class _FakeAdapter:
        async def pin_chat_message(self, chat_id, message_id, unpin=False):
            calls.append(("pin", chat_id, message_id, unpin))

        async def delete_message(self, chat_id, message_id):
            calls.append(("delete", chat_id, message_id))

    runner_ns._delivery_adapter_for = lambda source: _FakeAdapter()
    turn_runner = _turn_runner(runner_ns, agent=agent)
    turn_runner._ctx.source = SimpleNamespace(chat_id="111")
    turn_runner._ctx._loop_for_step = object()
    scheduled = []
    turn_runner._schedule = lambda coro, msg: scheduled.append((coro, msg))
    return record, agent, turn_runner, scheduled, calls


def test_finish_done_run_clears_the_flag_and_unpins(launch_home, autonomous_off):
    """The agent declared the run done mid-turn: disarm clears the live
    flag and hops the unpin + delete onto the gateway loop."""
    record, agent, turn_runner, scheduled, calls = _finish_harness(launch_home)
    autonomous_turn.begin(turn_runner)
    prev_budget = autonomous_turn.arm_conversation(turn_runner, agent, SESSION_KEY)
    store.mark_done(launch_home, record["run_id"])
    autonomous_turn.disarm_conversation(turn_runner, agent, SESSION_KEY, prev_budget)

    assert store.is_active(launch_home) is False
    assert store.load_run(launch_home, record["run_id"])["state"] == "done"
    assert len(scheduled) == 1
    assert scheduled[0][1] == "Autonomous pinned unpin"
    asyncio.run(scheduled[0][0])
    assert ("pin", "111", "123", True) in calls
    assert ("delete", "111", "123") in calls


def test_finish_done_run_leaves_a_waiting_run_alone(launch_home, autonomous_off):
    """needs_input ends the turn but not the mode: flag and pin stay, and
    nothing is scheduled — the next message resumes the run."""
    record, agent, turn_runner, scheduled, calls = _finish_harness(launch_home)
    autonomous_turn.begin(turn_runner)
    prev_budget = autonomous_turn.arm_conversation(turn_runner, agent, SESSION_KEY)
    store.mark_needs_input(launch_home, record["run_id"])
    autonomous_turn.disarm_conversation(turn_runner, agent, SESSION_KEY, prev_budget)

    assert store.is_active(launch_home) is True
    assert store.load_run(launch_home, record["run_id"])["state"] == "needs_input"
    assert scheduled == []
    assert calls == []


def test_finish_done_run_is_noop_when_the_run_is_still_going(launch_home, autonomous_off):
    record, agent, turn_runner, scheduled, calls = _finish_harness(launch_home)
    autonomous_turn.begin(turn_runner)
    prev_budget = autonomous_turn.arm_conversation(turn_runner, agent, SESSION_KEY)
    autonomous_turn.disarm_conversation(turn_runner, agent, SESSION_KEY, prev_budget)

    assert store.is_active(launch_home) is True
    assert store.load_run(launch_home, record["run_id"])["state"] == "running"
    assert scheduled == []
    assert calls == []
