"""Behaviour contracts for autonomous-mode boot wiring.

A restart never disables the mode (``gateway/run_startup_autonomous.py``):
the interrupted run is marked failed and the live flag + pin stay as they
were, so the pinned enable card still claims a mode that is on. The
auto-resume drain never restarts an autonomous turn
(``gateway/run_startup.py::_schedule_resume_pending_sessions``): the drain
leaves the session for the resume ask. A session with no autonomous run
drains normally.
"""
from __future__ import annotations

import pytest

import gateway.run as gateway_run
from gateway import autonomous_state as store
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _make_source(chat_id="123", user_id="u1"):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, user_id=user_id)


def _make_store(tmp_path):
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


class _FakeAdapter:
    """Delivery adapter that records unpins + deletes."""

    def __init__(self):
        self.pins = []
        self.deletes = []

    async def pin_chat_message(self, chat_id, message_id, *, unpin=False):
        self.pins.append((str(chat_id), str(message_id), unpin))
        return {"message_id": str(message_id), "pinned": not unpin}

    async def delete_message(self, chat_id, message_id):
        self.deletes.append((str(chat_id), str(message_id)))
        return True


def _make_runner(session_store, *, restored_source=None, adapter=None):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = session_store
    runner._is_session_running = lambda key: False
    runner._restored_source = lambda entry: restored_source
    runner._delivery_adapter_for = lambda source: adapter
    runner._resume_model_allows = lambda entry, source: True
    runner._restart_loop_guard_config = lambda: (0, 60, 300)
    return runner


@pytest.fixture
def auto_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


def _live_run(auto_home, session_key, *, pinned_message_id=777, chat_id="123"):
    record = store.create_run(auto_home, session_key, "Finish the goal")
    store.start_run(auto_home, record["run_id"])
    store.set_active(auto_home, True, run_id=record["run_id"],
                     pinned_message_id=pinned_message_id, chat_id=chat_id)
    return record


@pytest.mark.asyncio
async def test_boot_marks_run_failed_keeps_flag_and_pin(tmp_path, auto_home):
    from gateway.run_startup_autonomous import fail_interrupted_autonomous_run_on_boot

    session_store = _make_store(tmp_path)
    entry = session_store.get_or_create_session(_make_source())
    record = _live_run(auto_home, entry.session_key)
    adapter = _FakeAdapter()
    runner = _make_runner(session_store, restored_source=_make_source(), adapter=adapter)

    await fail_interrupted_autonomous_run_on_boot(runner)

    assert store.is_active(auto_home) is True
    assert store.load_run(auto_home, record["run_id"])["state"] == "failed"
    state = store.load_state(auto_home)
    assert state["current_run_id"] == record["run_id"]
    assert state["pinned_message_id"] == 777 and state["chat_id"] == "123"
    assert adapter.pins == [] and adapter.deletes == []


@pytest.mark.asyncio
async def test_boot_leaves_waiting_run_waiting_and_flag_on(tmp_path, auto_home):
    from gateway.run_startup_autonomous import fail_interrupted_autonomous_run_on_boot

    session_store = _make_store(tmp_path)
    entry = session_store.get_or_create_session(_make_source())
    record = _live_run(auto_home, entry.session_key)
    store.mark_needs_input(auto_home, record["run_id"])

    await fail_interrupted_autonomous_run_on_boot(_make_runner(session_store))

    assert store.is_active(auto_home) is True
    assert store.load_run(auto_home, record["run_id"])["state"] == "needs_input"


def test_drain_skips_autonomous_session(tmp_path, auto_home):
    session_store = _make_store(tmp_path)
    entry = session_store.get_or_create_session(_make_source())
    session_store.mark_resume_pending(entry.session_key)
    _live_run(auto_home, entry.session_key)
    # Boot marking already ran (the record is failed); the drain must not
    # restart the turn — and must not even resolve the source.
    store.fail_interrupted_run_on_boot(auto_home)

    def _must_not_be_called(entry):
        raise AssertionError("drain must not resolve the source for an autonomous session")

    runner = _make_runner(session_store)
    runner._restored_source = _must_not_be_called

    scheduled = runner._schedule_resume_pending_sessions()

    assert scheduled == 0


def test_drain_proceeds_past_blocker_without_autonomous_run(tmp_path, auto_home):
    session_store = _make_store(tmp_path)
    entry = session_store.get_or_create_session(_make_source())
    session_store.mark_resume_pending(entry.session_key)
    calls = []

    def _record_source(entry):
        calls.append(entry)
        return _make_source()

    runner = _make_runner(session_store, adapter=None)
    runner._restored_source = _record_source
    # No autonomous record: the drain reaches the adapter check and stops
    # there (no adapter) — proving the blocker let it through.

    scheduled = runner._schedule_resume_pending_sessions()

    assert scheduled == 0
    assert len(calls) == 1
