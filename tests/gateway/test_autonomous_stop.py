"""Behaviour contracts: /stop ends an autonomous run.

The owner stopping from their DM — mid-run (busy path) or between turns
(idle path) — ends the run the same way /autonomous disable does: the
record is kept as ``stopped`` (the resume source), the live flag clears,
the pin is unpinned + deleted, and the master setting clears so the
settings row agrees with the live state. Any other actor, chat, or
platform gets the plain /stop behaviour with the run untouched, and the
mode's existence never leaks.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway import autonomous_state
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, SessionStore
from plugins.platforms.telegram.autonomous_callback import stop_run_for_stop

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


def _make_runner(adapter):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._delivery_adapter_for = lambda source: adapter
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
    autonomous_state.set_active(home, True, run_id=record["run_id"],
                                pinned_message_id=pinned_message_id, chat_id=chat_id)
    (home / "config.yaml").write_text(
        "platforms:\n  telegram:\n    autonomous_mode: true\n", encoding="utf-8")
    return record


@pytest.mark.asyncio
async def test_stop_from_owner_dm_ends_run_clears_setting_and_pin(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = _live_run(auto_home)

    await stop_run_for_stop(runner, _make_source())

    assert autonomous_state.is_active(auto_home) is False
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "stopped"
    assert _master_mode(auto_home) is False
    assert (OWNER_ID, "777", True) in adapter.pins
    assert (OWNER_ID, "777") in adapter.deletes


@pytest.mark.asyncio
async def test_stop_with_mode_off_touches_nothing(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    await stop_run_for_stop(runner, _make_source())

    assert autonomous_state.is_active(auto_home) is False
    assert adapter.pins == [] and adapter.deletes == []


@pytest.mark.asyncio
async def test_stop_from_group_leaves_run_live(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = _live_run(auto_home)

    await stop_run_for_stop(runner, _make_source(chat_id="-1002397157264", chat_type="group"))

    assert autonomous_state.is_active(auto_home) is True
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "running"
    assert _master_mode(auto_home) is True


@pytest.mark.asyncio
async def test_stop_from_non_owner_leaves_run_live(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = _live_run(auto_home)

    await stop_run_for_stop(runner, _make_source(user_id=OTHER_ID, chat_id=OTHER_ID))

    assert autonomous_state.is_active(auto_home) is True
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "running"


@pytest.mark.asyncio
async def test_stop_from_other_platform_leaves_run_live(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = _live_run(auto_home)

    await stop_run_for_stop(runner, _make_source(platform=Platform.DISCORD))

    assert autonomous_state.is_active(auto_home) is True
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "running"


@pytest.mark.asyncio
async def test_busy_stop_ends_run(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    runner._interrupt_and_clear_session = AsyncMock()
    record = _live_run(auto_home)
    event = MessageEvent(text="/stop", source=_make_source())

    result = await runner._busy_stop_command(event, SESSION_KEY, _make_source())

    assert runner._interrupt_and_clear_session.await_count == 1
    assert autonomous_state.is_active(auto_home) is False
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "stopped"
    assert "topped" in str(result)


@pytest.mark.asyncio
async def test_idle_stop_ends_run(tmp_path, auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    runner._hm_stop_owner_turn = AsyncMock()
    runner._running_agents = {}
    runner.adapters = {}
    # The real async facade builds from the session store; the owner DM
    # source resolves to the same key the live run carries.
    runner.session_store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    record = _live_run(auto_home)
    event = MessageEvent(text="/stop", source=_make_source())

    result = await runner._handle_stop_command(event)

    assert runner._hm_stop_owner_turn.await_count == 1
    assert autonomous_state.is_active(auto_home) is False
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "stopped"
    assert isinstance(result, str)
