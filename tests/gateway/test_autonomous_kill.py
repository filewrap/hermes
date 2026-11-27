"""Behaviour contracts: /autonomous kill ends everything, for good.

A kill is not a disable. Disable stops the flag and keeps the run as a
resume source; a kill ends the coordinator turn, every live worker, the
queued work behind them, and any automatic revival — and the run is never
offered as Resume again. The owner gets one message saying what died and
the final token count. Any other actor, chat, or platform gets nothing
and the run is untouched.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from agent.i18n import t
from gateway import autonomous_state
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, SessionStore
from plugins.platforms.telegram.autonomous_callback import kill_autonomous_run

OWNER_ID = "8495104954"
OTHER_ID = "12345"
SESSION_KEY = "agent:main:telegram:dm:8495104954"


def _make_source(*, user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm",
                 platform=Platform.TELEGRAM):
    return SessionSource(
        platform=platform, user_id=user_id, chat_id=chat_id,
        user_name="owner", chat_type=chat_type,
    )


class _FakeAdapter:
    """Owner-checking adapter; records pins/deletes."""

    def __init__(self):
        self.pins = []
        self.deletes = []

    def _is_owner_user(self, user_id, source=None):
        return str(user_id) == OWNER_ID

    async def pin_chat_message(self, chat_id, message_id, *, unpin=False):
        self.pins.append((str(chat_id), str(message_id), unpin))
        return {"message_id": str(message_id), "pinned": not unpin}

    async def delete_message(self, chat_id, message_id):
        self.deletes.append((str(chat_id), str(message_id)))
        return True


def _make_runner(adapter, *, interrupt=None):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._delivery_adapter_for = lambda source: adapter
    runner._interrupt_and_clear_session = interrupt or AsyncMock()
    return runner


@pytest.fixture
def auto_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


def _master_mode(home):
    from hermes_cli.config import read_user_config_raw

    config = read_user_config_raw(home / "config.yaml")
    return bool((config.get("platforms") or {}).get("telegram", {}).get("autonomous_mode", False))


def _live_run(home, *, pinned_message_id=777, chat_id=OWNER_ID):
    record = autonomous_state.create_run(home, SESSION_KEY, "Finish the goal")
    autonomous_state.start_run(home, record["run_id"])
    autonomous_state.checkpoint(home, record["run_id"],
                                input_tokens=1000, output_tokens=500)
    autonomous_state.set_active(home, True, run_id=record["run_id"],
                                pinned_message_id=pinned_message_id, chat_id=chat_id)
    (home / "config.yaml").write_text(
        "platforms:\n  telegram:\n    autonomous_mode: true\n", encoding="utf-8")
    return record


# --- State layer: a killed run never comes back -------------------------------


def test_killed_running_run_is_not_resumable(auto_home):
    record = autonomous_state.create_run(auto_home, SESSION_KEY, "goal")
    autonomous_state.start_run(auto_home, record["run_id"])

    killed = autonomous_state.mark_killed(auto_home, record["run_id"])

    assert killed["state"] == "stopped"
    assert killed["stopped_by"] == "kill"
    assert killed["killed"] is True
    assert autonomous_state.find_resumable_run(auto_home) is None


def test_killed_failed_run_is_not_resumable(auto_home):
    record = autonomous_state.create_run(auto_home, SESSION_KEY, "goal")
    autonomous_state.start_run(auto_home, record["run_id"])
    autonomous_state.mark_failed(auto_home, record["run_id"])

    killed = autonomous_state.mark_killed(auto_home, record["run_id"])

    assert killed["state"] == "failed"
    assert killed["killed"] is True
    assert autonomous_state.find_resumable_run(auto_home) is None


def test_reopen_refuses_killed_run(auto_home):
    record = autonomous_state.create_run(auto_home, SESSION_KEY, "goal")
    autonomous_state.start_run(auto_home, record["run_id"])
    autonomous_state.mark_killed(auto_home, record["run_id"])

    with pytest.raises(ValueError):
        autonomous_state.reopen_run(auto_home, record["run_id"])


def test_ordinary_stopped_run_stays_resumable(auto_home):
    record = autonomous_state.create_run(auto_home, SESSION_KEY, "goal")
    autonomous_state.start_run(auto_home, record["run_id"])
    autonomous_state.mark_stopped(auto_home, record["run_id"])

    found = autonomous_state.find_resumable_run(auto_home)

    assert found is not None and found["run_id"] == record["run_id"]
    assert autonomous_state.reopen_run(auto_home, record["run_id"])["state"] == "running"


# --- Orchestration: kill ends the turn, the workers, the revival ------------


@pytest.mark.asyncio
async def test_kill_from_owner_dm_ends_run_and_reports(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = _live_run(auto_home)

    summary = await kill_autonomous_run(runner, _make_source())

    assert summary["killed"] is True
    assert summary["workers_interrupted"] == 0
    assert summary["tokens"] == 1500
    assert autonomous_state.is_active(auto_home) is False
    killed = autonomous_state.load_run(auto_home, record["run_id"])
    assert killed["state"] == "stopped"
    assert killed["stopped_by"] == "kill"
    assert killed["killed"] is True
    assert _master_mode(auto_home) is False
    assert (OWNER_ID, "777", True) in adapter.pins
    assert (OWNER_ID, "777") in adapter.deletes
    assert runner._interrupt_and_clear_session.await_count == 1


@pytest.mark.asyncio
async def test_kill_with_mode_off_touches_nothing(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    summary = await kill_autonomous_run(runner, _make_source())

    assert summary["killed"] is False
    assert adapter.pins == [] and adapter.deletes == []
    assert runner._interrupt_and_clear_session.await_count == 0


@pytest.mark.asyncio
async def test_kill_from_group_leaves_run_live(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = _live_run(auto_home)

    summary = await kill_autonomous_run(
        runner, _make_source(chat_id="-1002397157264", chat_type="group"))

    assert summary["killed"] is False
    assert autonomous_state.is_active(auto_home) is True
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "running"


@pytest.mark.asyncio
async def test_kill_from_non_owner_leaves_run_live(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = _live_run(auto_home)

    summary = await kill_autonomous_run(
        runner, _make_source(user_id=OTHER_ID, chat_id=OTHER_ID))

    assert summary["killed"] is False
    assert autonomous_state.is_active(auto_home) is True
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "running"


@pytest.mark.asyncio
async def test_kill_counts_live_workers(monkeypatch, auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    runner._peek_session_state = lambda key: SimpleNamespace(
        turn=SimpleNamespace(agent=SimpleNamespace(
            _active_children=[object(), object()], session_id="sess-1")))
    _live_run(auto_home)
    calls = []

    def _fake_interrupt_for_session(*, session_key="", **kwargs):
        calls.append(session_key)
        return 3

    monkeypatch.setattr(
        "tools.async_delegation.interrupt_for_session", _fake_interrupt_for_session)

    summary = await kill_autonomous_run(runner, _make_source())

    assert calls == [SESSION_KEY]
    assert summary["workers_interrupted"] == 5
    assert summary["async_interrupted"] == 3
    assert summary["sync_children"] == 2


# --- Command wiring: /autonomous kill mid-run and idle -----------------------


@pytest.mark.asyncio
async def test_busy_autonomous_kill(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = _live_run(auto_home)
    event = MessageEvent(text="/autonomous kill", source=_make_source())

    result = await runner._busy_autonomous_command(event, SESSION_KEY, _make_source())

    assert runner._interrupt_and_clear_session.await_count == 1
    assert autonomous_state.is_active(auto_home) is False
    killed = autonomous_state.load_run(auto_home, record["run_id"])
    assert killed["killed"] is True
    assert "kill" in str(result).lower()


@pytest.mark.asyncio
async def test_busy_autonomous_other_args_reject(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = _live_run(auto_home)
    event = MessageEvent(text="/autonomous enable", source=_make_source())

    result = await runner._busy_autonomous_command(event, SESSION_KEY, _make_source())

    assert result == t("gateway.busy.reject_autonomous")
    assert runner._interrupt_and_clear_session.await_count == 0
    assert autonomous_state.is_active(auto_home) is True
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "running"


@pytest.mark.asyncio
async def test_idle_autonomous_kill_command(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    runner._session_key_for_source = lambda source: SESSION_KEY
    runner._normalize_source_for_session_key = lambda source: source
    runner._reply_anchor_for_event = lambda event: None
    record = autonomous_state.create_run(auto_home, SESSION_KEY, "Finish the goal")
    autonomous_state.start_run(auto_home, record["run_id"])
    autonomous_state.mark_needs_input(auto_home, record["run_id"])
    event = MessageEvent(text="/autonomous kill", source=_make_source())

    result = await runner._handle_autonomous_command(event)

    assert isinstance(result, str) and "kill" in result.lower()
    assert autonomous_state.is_active(auto_home) is False
    assert autonomous_state.load_run(auto_home, record["run_id"])["killed"] is True


# --- Stale-flag clear routes through disable ---------------------------------


def test_begin_stale_flag_clears_through_disable(tmp_path, monkeypatch):
    """A DONE run under a live flag (a crash between the end and the flag
    clear) still clears through disable(): the one flag-clear path. A
    stopped/failed run is NOT stale — the mode stays on (see
    test_begin_keeps_flag_for_a_stopped_run), so it is not covered here."""
    from gateway import autonomous_turn
    from gateway.run_turn_runner import TurnRunner

    home = tmp_path / "launch"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    record = autonomous_state.create_run(home, SESSION_KEY, "goal")
    autonomous_state.start_run(home, record["run_id"])
    autonomous_state.mark_done(home, record["run_id"])
    autonomous_state.set_active(home, True, run_id=record["run_id"])

    turn_runner = object.__new__(TurnRunner)
    turn_runner._runner = SimpleNamespace(_agent_cache={}, _agent_cache_lock=threading.Lock())
    turn_runner._ctx = SimpleNamespace(
        session_key=SESSION_KEY, message="hi",
        agent_holder=[SimpleNamespace(model="m", provider="p")])
    autonomous_turn.begin(turn_runner)

    assert autonomous_state.is_active(home) is False
    assert turn_runner._autonomous_active is False
    assert autonomous_state.load_run(home, record["run_id"])["state"] == "done"
