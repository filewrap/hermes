"""Behaviour contracts: /autonomous disable clears the master setting.

Disabling from either surface — the ``/autonomous disable`` handler or
the ``hm:autonomous:disable`` button — must also clear the persisted
``platforms.telegram.autonomous_mode`` key, so the settings row agrees
with the live state after a command-disable. A failed config write never
turns the disable itself into an error: the live flag is already off.
"""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from agent.i18n import t
from gateway import autonomous_state
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

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


@dataclass(frozen=True)
class _SettingsSource:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    profile: str | None = None


def _settings_row_text(home):
    """The settings home view's Autonomous mode row for *home*."""
    adapter = SimpleNamespace()
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: home)
    ui = TelegramPersonalSettingsUI(adapter)
    source = _SettingsSource()
    state = ui._new_state(source, home, message_id=55)
    state["user_name"] = source.user_name
    text, _markup = ui._home_view(state)
    return text, state["id"]


def _assert_row_off(home):
    text, _state_id = _settings_row_text(home)
    assert ('<td><b>Autonomous mode</b> <tg-button type="callback_data" style="primary" '
            'data="badge:new">new</tg-button></td><td>Off</td>') in text


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
async def test_handler_disable_clears_master_setting_and_row_shows_off(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await runner._handle_autonomous_command(_make_event("/autonomous enable"))
    assert _master_mode(auto_home) is True

    result = await runner._handle_autonomous_command(_make_event("/autonomous disable"))

    assert result == t("gateway.autonomous.disabled")
    assert autonomous_state.is_active(auto_home) is False
    assert _master_mode(auto_home) is False
    _assert_row_off(auto_home)


@pytest.mark.asyncio
async def test_handler_disable_clears_stale_setting_with_no_live_run(auto_home):
    """The restart case: the setting survives a reboot (by design) while the
    live flag is off; a disable with no live run still clears the setting."""
    TelegramPersonalSettingsUI._set_autonomous_mode(auto_home, True)
    assert _master_mode(auto_home) is True
    assert autonomous_state.is_active(auto_home) is False
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    result = await runner._handle_autonomous_command(_make_event("/autonomous disable"))

    assert result == t("gateway.autonomous.disabled")
    assert _master_mode(auto_home) is False
    _assert_row_off(auto_home)


@pytest.mark.asyncio
async def test_handler_disable_survives_setting_write_failure(auto_home, monkeypatch):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await runner._handle_autonomous_command(_make_event("/autonomous enable"))
    monkeypatch.setattr(
        TelegramPersonalSettingsUI, "_set_autonomous_mode",
        staticmethod(lambda home, enabled: (_ for _ in ()).throw(RuntimeError("disk full"))))

    result = await runner._handle_autonomous_command(_make_event("/autonomous disable"))

    # The disable stands — the live flag is off and the owner is told so;
    # only the config write failed, so the stale setting stays on.
    assert result == t("gateway.autonomous.disabled")
    assert autonomous_state.is_active(auto_home) is False
    assert _master_mode(auto_home) is True


@pytest.mark.asyncio
async def test_callback_disable_clears_master_setting_and_row_shows_off(auto_home):
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await runner._handle_autonomous_command(_make_event("/autonomous enable"))
    assert _master_mode(auto_home) is True
    event, source, answered = _callback_event(b"hm:autonomous:disable")

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:disable")

    assert handled is True
    assert answered == [(("Autonomous mode disabled.",), {})]
    assert autonomous_state.is_active(auto_home) is False
    assert _master_mode(auto_home) is False
    _assert_row_off(auto_home)


@pytest.mark.asyncio
async def test_callback_disable_non_owner_leaves_setting_on(auto_home):
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await runner._handle_autonomous_command(_make_event("/autonomous enable"))
    event, source, answered = _callback_event(
        b"hm:autonomous:disable", user_id=OTHER_ID, chat_id=OTHER_ID)

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:disable")

    assert handled is True
    assert answered == [((), {})]
    assert autonomous_state.is_active(auto_home) is True
    assert _master_mode(auto_home) is True
