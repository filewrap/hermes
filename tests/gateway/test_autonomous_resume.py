"""Behaviour contracts for the autonomous resume ask.

Re-enabling with a previous stopped/failed/waiting run asks first: a card
naming the run (goal, where it stopped, steps, tokens) with Resume (re-open
the record) or Start fresh (a new run on the same goal). Nothing changes
until the owner chooses — no setting flip, no run created. A live run
skips the ask (re-enable replaces it), and a tap on a stale run answers
that it is unavailable instead of starting anything.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from agent.i18n import t
from gateway import autonomous_state
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

OWNER_ID = "8495104954"
OTHER_ID = "12345"
SESSION_KEY = "agent:main:telegram:dm:8495104954"


def _make_event(text, *, user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm",
                platform=Platform.TELEGRAM):
    source = SessionSource(
        platform=platform, user_id=user_id, chat_id=chat_id,
        user_name="owner", chat_type=chat_type,
    )
    return MessageEvent(text=text, source=source)


class _FakeAdapter:
    """Owner-checking adapter with a card method; records pins/deletes."""

    def __init__(self, *, card_message_id="777"):
        self.cards = []
        self.pins = []
        self.deletes = []
        self.card_message_id = card_message_id

    def _is_owner_user(self, user_id, source=None):
        return str(user_id) == OWNER_ID

    async def send_command_card(self, *, source, html, links=(), copies=(),
                                buttons=(), reply_to=None):
        self.cards.append({"html": html, "buttons": buttons})
        return self.card_message_id

    async def pin_chat_message(self, chat_id, message_id, *, unpin=False):
        self.pins.append((str(chat_id), str(message_id), unpin))
        return {"message_id": str(message_id), "pinned": not unpin}

    async def delete_message(self, chat_id, message_id):
        self.deletes.append((str(chat_id), str(message_id)))
        return True


def _make_runner(adapter):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._delivery_adapter_for = lambda source: adapter
    runner._session_key_for_source = lambda source: SESSION_KEY
    runner._normalize_source_for_session_key = lambda source: source
    runner._reply_anchor_for_event = lambda event: None
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


def _stopped_run(home, goal="Finish the goal"):
    record = autonomous_state.create_run(home, SESSION_KEY, goal)
    autonomous_state.start_run(home, record["run_id"])
    autonomous_state.mark_stopped(home, record["run_id"], stopped_by="owner")
    # Every production stop path also clears the live flag (disable()).
    autonomous_state.set_active(home, False)
    return record


def _callback_event(*, message_id=555):
    return SimpleNamespace(message_id=message_id, answer=AsyncMock())


@pytest.mark.asyncio
async def test_enable_asks_when_resumable_run_exists(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = _stopped_run(auto_home)

    result = await runner._handle_autonomous_command(_make_event("/autonomous enable"))

    assert result is None  # ask card sent; the runner sends nothing else
    assert len(adapter.cards) == 1
    card = adapter.cards[0]
    assert "Finish the goal" in card["html"]
    assert card["buttons"] == ()
    assert ">Resume</tg-button>" in card["html"]
    assert f'data="hm:autonomous:resume:{record["run_id"]}"' in card["html"]
    assert ">Start fresh</tg-button>" in card["html"]
    assert f'data="hm:autonomous:fresh:{record["run_id"]}"' in card["html"]
    # Nothing changed until the owner chooses.
    assert autonomous_state.is_active(auto_home) is False
    assert _master_mode(auto_home) is False
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "stopped"
    assert adapter.pins == []


@pytest.mark.asyncio
async def test_enable_starts_fresh_when_no_resumable_run(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    result = await runner._handle_autonomous_command(_make_event("/autonomous enable"))

    assert result is None
    assert len(adapter.cards) == 1
    assert adapter.cards[0]["buttons"] == ()
    assert ">Disable</tg-button>" in adapter.cards[0]["html"]
    assert autonomous_state.is_active(auto_home) is True


@pytest.mark.asyncio
async def test_enable_with_live_run_replaces_without_asking(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    old = _stopped_run(auto_home, goal="Old goal")
    live = autonomous_state.create_run(auto_home, SESSION_KEY, "Live goal")
    autonomous_state.start_run(auto_home, live["run_id"])

    result = await runner._handle_autonomous_command(_make_event("/autonomous enable"))

    assert result is None
    assert len(adapter.cards) == 1
    # The enable card, not the ask card.
    assert adapter.cards[0]["buttons"] == ()
    assert ">Disable</tg-button>" in adapter.cards[0]["html"]
    assert autonomous_state.load_run(auto_home, live["run_id"])["state"] == "stopped"
    assert autonomous_state.load_run(auto_home, old["run_id"])["state"] == "stopped"
    assert autonomous_state.is_active(auto_home) is True


@pytest.mark.asyncio
async def test_resume_tap_reopens_run_and_pins_enable_card(auto_home):
    adapter = _FakeAdapter()
    record = _stopped_run(auto_home)
    event = _callback_event()
    source = _make_event("/autonomous enable").source

    handled = await handle_autonomous_callback(
        adapter, event, source, f"hm:autonomous:resume:{record['run_id']}")

    assert handled is True
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "running"
    assert autonomous_state.is_active(auto_home) is True
    assert _master_mode(auto_home) is True
    assert (OWNER_ID, "555") in adapter.deletes  # the ask message is gone
    assert len(adapter.cards) == 1
    assert adapter.cards[0]["buttons"] == ()
    assert ">Disable</tg-button>" in adapter.cards[0]["html"]
    assert adapter.pins == [(OWNER_ID, "777", False)]


@pytest.mark.asyncio
async def test_fresh_tap_starts_new_run_on_same_goal(auto_home):
    adapter = _FakeAdapter()
    old = _stopped_run(auto_home)
    event = _callback_event()
    source = _make_event("/autonomous enable").source

    handled = await handle_autonomous_callback(
        adapter, event, source, f"hm:autonomous:fresh:{old['run_id']}")

    assert handled is True
    assert autonomous_state.load_run(auto_home, old["run_id"])["state"] == "stopped"
    state = autonomous_state.load_state(auto_home)
    new_id = state["current_run_id"]
    assert new_id is not None and new_id != old["run_id"]
    new = autonomous_state.load_run(auto_home, new_id)
    assert new["state"] == "running"
    assert new["goal"] == "Finish the goal"
    assert new["session_key"] == SESSION_KEY
    assert adapter.pins == [(OWNER_ID, "777", False)]


@pytest.mark.asyncio
async def test_router_dispatches_resume_tap_to_choice_handler(auto_home):
    from plugins.platforms.telegram.hm_action_callbacks import handle_hm_action_callback

    adapter = _FakeAdapter()
    record = _stopped_run(auto_home)
    event = _callback_event()
    source = _make_event("/autonomous enable").source

    await handle_hm_action_callback(
        adapter, event, source, f"hm:autonomous:resume:{record['run_id']}")

    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "running"
    assert autonomous_state.is_active(auto_home) is True


@pytest.mark.asyncio
async def test_choice_tap_from_non_owner_is_silent(auto_home):
    adapter = _FakeAdapter()
    record = _stopped_run(auto_home)
    event = _callback_event()
    source = _make_event("/x", user_id=OTHER_ID, chat_id=OTHER_ID).source

    handled = await handle_autonomous_callback(
        adapter, event, source, f"hm:autonomous:resume:{record['run_id']}")

    assert handled is True
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "stopped"
    assert autonomous_state.is_active(auto_home) is False
    assert adapter.cards == [] and adapter.pins == []
    event.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_choice_tap_with_missing_run_answers_unavailable(auto_home):
    adapter = _FakeAdapter()
    event = _callback_event()
    source = _make_event("/autonomous enable").source

    handled = await handle_autonomous_callback(
        adapter, event, source, "hm:autonomous:resume:run_nope")

    assert handled is True
    assert autonomous_state.is_active(auto_home) is False
    assert adapter.cards == []
    event.answer.assert_awaited_once_with(t("gateway.autonomous.resume_unavailable"))
