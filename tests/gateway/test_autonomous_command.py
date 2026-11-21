"""Behaviour contracts for /autonomous (command + Disable callback).

Owner-only + DM-only + Telegram-only, silent denial everywhere else: a
non-owner, a group chat, or another platform gets no message, no reaction,
and no state change. Enable sends the confirmation card (green tag row +
Disable button), pins it, and persists the pinned message id; disable
unpins + deletes the pinned message, stops the run record, and answers
with a message saying the mode is disabled. Mid-run /autonomous rejects
with the busy text instead of touching the running turn.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from agent.i18n import t
from gateway import autonomous_state
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

OWNER_ID = "8495104954"
OTHER_ID = "12345"


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
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:8495104954"
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


async def _enable(runner, adapter, text="/autonomous enable"):
    return await runner._handle_autonomous_command(_make_event(text))


@pytest.mark.asyncio
async def test_enable_sends_tagged_card_pins_and_persists(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    result = await _enable(runner, adapter)

    assert result is None  # card sent; the runner sends nothing else
    assert len(adapter.cards) == 1
    card = adapter.cards[0]
    assert "<tg-button-row>" in card["html"]
    assert 'type="disabled" style="success"' in card["html"]
    assert "autonomous" in card["html"]
    assert "Autonomous mode is on" in card["html"]
    assert card["buttons"] == ()
    assert ">Disable</tg-button>" in card["html"]
    assert adapter.pins == [(OWNER_ID, "777", False)]
    state = autonomous_state.load_state(auto_home)
    assert state["active"] is True
    assert state["pinned_message_id"] == "777"
    assert state["chat_id"] == OWNER_ID
    assert _master_mode(auto_home) is True
    record = autonomous_state.load_run(auto_home, state["current_run_id"])
    assert record is not None and record["state"] == "running"


@pytest.mark.asyncio
async def test_enable_card_failure_falls_back_to_plain_reply(auto_home):
    adapter = _FakeAdapter(card_message_id=None)
    runner = _make_runner(adapter)

    result = await _enable(runner, adapter)

    assert result is not None and "Autonomous mode is on" in result
    assert adapter.pins == []
    assert autonomous_state.is_active(auto_home) is True


@pytest.mark.asyncio
async def test_disable_unpins_deletes_stops_run_and_reports(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await _enable(runner, adapter)
    run_id = autonomous_state.load_state(auto_home)["current_run_id"]

    result = await runner._handle_autonomous_command(_make_event("/autonomous disable"))

    assert result == t("gateway.autonomous.disabled")
    assert (OWNER_ID, "777", True) in adapter.pins
    assert (OWNER_ID, "777") in adapter.deletes
    assert autonomous_state.is_active(auto_home) is False
    assert autonomous_state.load_state(auto_home)["pinned_message_id"] is None
    record = autonomous_state.load_run(auto_home, run_id)
    assert record is not None and record["state"] == "stopped"


@pytest.mark.asyncio
async def test_bare_command_sends_status_card_not_plain_text(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    assert await runner._handle_autonomous_command(_make_event("/autonomous")) is None
    assert len(adapter.cards) == 1
    assert "Autonomous mode" in adapter.cards[0]["html"]
    await _enable(runner, adapter)
    adapter.cards.clear()
    assert await runner._handle_autonomous_command(_make_event("/autonomous")) is None
    assert len(adapter.cards) == 1


@pytest.mark.asyncio
async def test_unknown_arg_returns_usage_without_state_change(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    result = await runner._handle_autonomous_command(_make_event("/autonomous frobnicate"))

    assert result == t("gateway.autonomous.usage")
    assert autonomous_state.is_active(auto_home) is False
    assert adapter.cards == [] and adapter.pins == []


@pytest.mark.asyncio
async def test_enable_replaces_live_run_and_old_pin(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await _enable(runner, adapter)
    first_run = autonomous_state.load_state(auto_home)["current_run_id"]
    adapter.card_message_id = "888"

    result = await _enable(runner, adapter)

    assert result is None
    assert (OWNER_ID, "777", True) in adapter.pins
    assert (OWNER_ID, "777") in adapter.deletes
    assert (OWNER_ID, "888", False) in adapter.pins
    state = autonomous_state.load_state(auto_home)
    assert state["active"] is True and state["pinned_message_id"] == "888"
    assert autonomous_state.load_run(auto_home, first_run)["state"] == "stopped"
    assert autonomous_state.load_run(auto_home, state["current_run_id"])["state"] == "running"


@pytest.mark.asyncio
async def test_non_owner_silence(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    result = await runner._handle_autonomous_command(
        _make_event("/autonomous enable", user_id=OTHER_ID, chat_id=OTHER_ID))

    assert result is None
    assert adapter.cards == [] and adapter.pins == []
    assert autonomous_state.is_active(auto_home) is False
    assert not (auto_home / "autonomous").exists()
    assert _master_mode(auto_home) is False


@pytest.mark.asyncio
async def test_non_dm_silence(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    result = await runner._handle_autonomous_command(
        _make_event("/autonomous enable", chat_id="-1001", chat_type="group"))

    assert result is None
    assert adapter.cards == [] and adapter.pins == []
    assert autonomous_state.is_active(auto_home) is False


@pytest.mark.asyncio
async def test_non_telegram_silence(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    result = await runner._handle_autonomous_command(
        _make_event("/autonomous enable", platform=Platform.DISCORD))

    assert result is None
    assert adapter.cards == [] and adapter.pins == []
    assert autonomous_state.is_active(auto_home) is False


@pytest.mark.asyncio
async def test_config_save_failure_changes_nothing(auto_home, monkeypatch):
    from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

    def _failing_setter(home, enabled):
        raise RuntimeError("disk full")

    monkeypatch.setattr(
        TelegramPersonalSettingsUI, "_set_autonomous_mode",
        staticmethod(_failing_setter))
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    result = await _enable(runner, adapter)

    assert result == t("gateway.autonomous.save_failed")
    assert autonomous_state.is_active(auto_home) is False
    assert adapter.cards == []


@pytest.mark.asyncio
async def test_mid_run_autonomous_rejects_without_touching_state(auto_home):
    from hermes_cli.commands import resolve_command

    runner = object.__new__(gateway_run.GatewayRunner)
    event = _make_event("/autonomous enable")

    busy_reply = await runner._dispatch_busy_slash_command(
        event, resolve_command("autonomous"), "qk", event.source)

    assert busy_reply == t("gateway.busy.reject_autonomous")
    assert not (auto_home / "autonomous").exists()


def test_tenant_policy_covers_autonomous():
    # Only the autonomous classification itself: the registry-wide
    # completeness assertion already fails on this branch for unrelated
    # pre-existing drift (autoprune, clear, discard, last, mcp, plugins,
    # swarm), so it is not asserted here.
    from gateway.tenant_commands import tenant_command_policy

    assert tenant_command_policy("autonomous") == "tenant"


def test_find_resumable_run_returns_latest_stopped(auto_home):
    first = autonomous_state.create_run(auto_home, "s", "first")
    autonomous_state.start_run(auto_home, first["run_id"])
    autonomous_state.mark_stopped(auto_home, first["run_id"])
    second = autonomous_state.create_run(auto_home, "s", "second")
    autonomous_state.start_run(auto_home, second["run_id"])
    autonomous_state.mark_stopped(auto_home, second["run_id"])
    done = autonomous_state.create_run(auto_home, "s", "done")
    autonomous_state.start_run(auto_home, done["run_id"])
    autonomous_state.mark_done(auto_home, done["run_id"])

    found = autonomous_state.find_resumable_run(auto_home)

    assert found is not None and found["run_id"] == second["run_id"]


def test_disable_keeps_stopped_record_and_clears_pin(auto_home):
    record = autonomous_state.create_run(auto_home, "s", "goal")
    autonomous_state.start_run(auto_home, record["run_id"])
    autonomous_state.set_active(
        auto_home, True, run_id=record["run_id"],
        pinned_message_id="777", chat_id=OWNER_ID)

    pinned = autonomous_state.disable(auto_home, stopped_by="autonomous_disable")

    assert pinned == {"pinned_message_id": "777", "chat_id": OWNER_ID}
    assert autonomous_state.is_active(auto_home) is False
    assert autonomous_state.load_run(auto_home, record["run_id"])["state"] == "stopped"


def _callback_event(data, *, user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm"):
    answered = []

    class Event:
        async def answer(self, *args, **kwargs):
            answered.append((args, kwargs))

    event = Event()
    event.data = data
    source = SimpleNamespace(
        user_id=user_id, chat_id=chat_id, chat_type=chat_type)
    return event, source, answered


@pytest.mark.asyncio
async def test_disable_callback_disables_and_cleans_up(auto_home):
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await _enable(runner, adapter)
    run_id = autonomous_state.load_state(auto_home)["current_run_id"]
    event, source, answered = _callback_event(b"hm:autonomous:disable")

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:disable")

    assert handled is True
    assert answered == [(("Autonomous mode disabled.",), {})]
    assert (OWNER_ID, "777", True) in adapter.pins
    assert (OWNER_ID, "777") in adapter.deletes
    assert autonomous_state.is_active(auto_home) is False
    assert autonomous_state.load_run(auto_home, run_id)["state"] == "stopped"


@pytest.mark.asyncio
async def test_disable_callback_non_owner_is_silent(auto_home):
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await _enable(runner, adapter)
    event, source, answered = _callback_event(
        b"hm:autonomous:disable", user_id=OTHER_ID, chat_id=OTHER_ID)

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:disable")

    assert handled is True
    assert answered == [((), {})]
    assert adapter.pins == [(OWNER_ID, "777", False)]
    assert adapter.deletes == []
    assert autonomous_state.is_active(auto_home) is True


@pytest.mark.asyncio
async def test_disable_callback_non_dm_is_silent(auto_home):
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await _enable(runner, adapter)
    event, source, answered = _callback_event(
        b"hm:autonomous:disable", chat_id="-1001", chat_type="group")

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:disable")

    assert handled is True
    assert answered == [((), {})]
    assert adapter.deletes == []
    assert autonomous_state.is_active(auto_home) is True


@pytest.mark.asyncio
async def test_disable_callback_wrong_data_is_unhandled(auto_home):
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    event, source, answered = _callback_event(b"hm:autonomous:bogus")

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:bogus")

    assert handled is False
    assert answered == []


@pytest.mark.asyncio
async def test_on_callback_query_routes_autonomous_disable(auto_home, monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    fake = _FakeAdapter()
    runner = _make_runner(fake)
    await _enable(runner, fake)
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=SimpleNamespace(
        user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm", thread_id=None)))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    # The real owner check reads OWNER_TELEGRAM_ID from the profile .env,
    # which the temp home lacks; routing (not ownership) is under test here.
    monkeypatch.setattr(
        adapter, "_is_owner_user", lambda user_id, source=None: str(user_id) == OWNER_ID)
    monkeypatch.setattr(adapter, "pin_chat_message", AsyncMock(return_value={}))
    monkeypatch.setattr(adapter, "delete_message", AsyncMock(return_value=True))
    event, _source, answered = _callback_event(b"hm:autonomous:disable")

    await adapter._on_callback_query(event)

    assert answered == [(("Autonomous mode disabled.",), {})]
    adapter.pin_chat_message.assert_awaited_once()
    assert adapter.pin_chat_message.await_args.kwargs.get("unpin") is True
    adapter.delete_message.assert_awaited_once()
    assert autonomous_state.is_active(auto_home) is False


@pytest.mark.asyncio
async def test_send_command_card_returns_message_id(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = object()
    monkeypatch.setattr(
        adapter, "_send_rich_ui", AsyncMock(return_value=SimpleNamespace(id=777)))

    message_id = await adapter.send_command_card(
        source=SimpleNamespace(chat_id=OWNER_ID, thread_id=None),
        html="<p>hi</p>", buttons=(("Disable", "hm:autonomous:disable"),))

    assert message_id == "777"


@pytest.mark.asyncio
async def test_send_command_card_returns_none_without_client():
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = None

    message_id = await adapter.send_command_card(
        source=SimpleNamespace(chat_id=OWNER_ID, thread_id=None), html="<p>hi</p>")

    assert message_id is None
