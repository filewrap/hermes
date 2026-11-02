"""Regression tests for setup navigation staying on one exact Telegram RichMessage card."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import pytest
from telethon import functions

from gateway.config import PlatformConfig
from plugins.platforms.telegram import control_center
from plugins.platforms.telegram.mtproto_rich import edit_request, inline_markup, send_request
from plugins.platforms.telegram.setup_ui import TelegramSetupUI
from plugins.platforms.telegram.standalone_ui import TelegramStandaloneUI
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.fixture
def routed_adapter(tmp_path):
    adapter = TelethonAdapter(PlatformConfig())
    home = tmp_path / "profile-alpha"
    home.mkdir()
    source = SimpleNamespace(
        chat_type="dm", chat_id="101", user_id="101", user_name="Owner",
        profile="alpha", thread_id="77",
    )
    adapter._settings_home = lambda _source: home
    adapter._peer_for = lambda _chat_id: "peer-101"
    adapter._authorized = lambda _source: True
    adapter._is_owner_user = lambda user_id, source=None: str(user_id) == "101"
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=77))
    adapter._control_route_store = {}
    setup_state = {
        "id": 9, "identity": (str(home), "alpha", "101", "101", "77"),
        "home": str(home), "profile": "alpha", "user_id": "101", "chat_id": "101",
        "thread_id": "77", "message_id": "77", "section": "home",
    }
    adapter._telegram_setup_ui = SimpleNamespace(
        _states={9: setup_state}, handle_callback=AsyncMock(),
    )
    return adapter, source, setup_state


@pytest.mark.asyncio
async def test_setup_destination_edits_origin_card_and_returns_exact_id(routed_adapter):
    adapter, source, _setup_state = routed_adapter
    bound = {}

    class SettingsCard:
        async def open(self, card_source, *, reply_to=None):
            request = send_request(
                adapter._peer_for(card_source.chat_id), "<h1>Personal settings</h1>",
                reply_to=reply_to, reply_markup=inline_markup([[('Refresh', 'ps:refresh', None)]]),
                rich_format="html",
            )
            receipt = await adapter._mtproto_rich_request(request)
            bound["message_id"] = adapter._sent_message_id(receipt)

    adapter._settings_ui = lambda: SettingsCard()
    event = SimpleNamespace(message_id=77)
    await adapter._dispatch_control_command(source, "settings", reply_to=77, event=event)

    request = adapter._client.await_args.args[0]
    assert isinstance(request, functions.messages.EditMessageRequest)
    assert request.id == 77
    assert request.rich_message.html.startswith("<h1>Personal settings</h1>")
    assert "Page 1 of 1" in request.rich_message.html
    assert bound["message_id"] == "77"
    labels = [button.text for row in request.reply_markup.rows for button in row.buttons]
    payloads = [button.type.data.decode() if isinstance(button.type.data, bytes) else button.type.data
                for row in request.reply_markup.rows for button in row.buttons]
    assert labels == ["Refresh", "← Back to setup"]
    assert payloads[-1].startswith("hm:setup-back:")


@pytest.mark.asyncio
async def test_nested_callback_edits_same_card_and_keeps_setup_return_route(routed_adapter):
    adapter, source, _setup_state = routed_adapter

    async def delegated_callback(event, callback_source, _data):
        await adapter._mtproto_rich_request(edit_request(
            adapter._peer_for(callback_source.chat_id), 999, "<h1>Dashboard status</h1>",
            reply_markup=inline_markup([[('Refresh', 'hm:standalone:10:dashboard', None)]]),
            rich_format="html",
        ))

    adapter._telegram_standalone_ui = SimpleNamespace(handle_callback=delegated_callback)
    # First establish the setup return route by entering a delegated screen.
    class DashboardCard:
        async def open(self, card_source, *, reply_to=None):
            await adapter._mtproto_rich_request(send_request(
                adapter._peer_for(card_source.chat_id), "<h1>Dashboard</h1>",
                reply_to=reply_to, reply_markup=inline_markup([[('Refresh', 'hm:standalone:10:dashboard', None)]]),
                rich_format="html",
            ))

    adapter._settings_ui = lambda: DashboardCard()
    await adapter._dispatch_control_command(source, "settings", reply_to=77,
                                             event=SimpleNamespace(message_id=77))
    callback = SimpleNamespace(message_id=77, answer=AsyncMock())
    assert await adapter._handle_control_callback(
        callback, source, "hm:standalone:10:dashboard",
    )

    request = adapter._client.await_args.args[0]
    assert isinstance(request, functions.messages.EditMessageRequest)
    assert request.id == 77
    assert request.rich_message.html.startswith("<h1>Dashboard status</h1>")
    assert "Page 1 of 1" in request.rich_message.html
    labels = [button.text for row in request.reply_markup.rows for button in row.buttons]
    assert labels == ["Refresh", "← Back to setup"]


@pytest.mark.asyncio
async def test_back_to_setup_rejects_wrong_actor_and_wrong_card(routed_adapter):
    adapter, source, _setup_state = routed_adapter
    class SettingsCard:
        async def open(self, card_source, *, reply_to=None):
            await adapter._mtproto_rich_request(send_request(
                adapter._peer_for(card_source.chat_id), "<h1>Settings</h1>",
                reply_to=reply_to, reply_markup=inline_markup([[('Refresh', 'ps:refresh', None)]]),
                rich_format="html",
            ))
    adapter._settings_ui = lambda: SettingsCard()
    await adapter._dispatch_control_command(
        source, "settings", reply_to=77, event=SimpleNamespace(message_id=77),
    )
    route = next(iter(adapter._control_route_store.values()))
    callback_data = f"hm:setup-back:{route['token']}"
    callback = SimpleNamespace(message_id=77, answer=AsyncMock())

    intruder = SimpleNamespace(**{**vars(source), "user_id": "202"})
    await adapter._handle_control_callback(callback, intruder, callback_data)
    adapter._telegram_setup_ui.handle_callback.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("alert") is True

    wrong_card = SimpleNamespace(message_id=78, answer=AsyncMock())
    await adapter._handle_control_callback(wrong_card, source, callback_data)
    adapter._telegram_setup_ui.handle_callback.assert_not_awaited()
    assert wrong_card.answer.await_args.kwargs.get("alert") is True


@pytest.mark.asyncio
async def test_real_setup_and_settings_controllers_return_to_origin_card(tmp_path):
    adapter = TelethonAdapter(PlatformConfig())
    home = tmp_path / "profile-real"
    home.mkdir()
    source = SimpleNamespace(
        chat_type="dm", chat_id="101", user_id="101", user_name="Owner",
        profile="alpha", thread_id="77",
    )
    adapter._settings_home = lambda _source: home
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: home)
    adapter._peer_for = lambda _chat_id: "peer-101"
    adapter._authorized = lambda _source: True
    adapter._is_owner_user = lambda user_id, source=None: str(user_id) == "101"
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=501))
    adapter._control_route_store = {}
    setup = adapter._telegram_setup_ui = TelegramSetupUI(adapter)

    # Open an actual setup card and let the real settings controller edit it.
    await setup.handle_command(source)
    setup_state = next(iter(setup._states.values()))
    assert setup_state["message_id"] == "501"
    callback = SimpleNamespace(message_id=501, answer=AsyncMock())
    await adapter._dispatch_control_command(source, "settings", reply_to=501, event=callback)

    delegated_edit = adapter._client.await_args.args[0]
    assert isinstance(delegated_edit, functions.messages.EditMessageRequest)
    assert delegated_edit.id == 501
    assert "Personal settings" in delegated_edit.message
    assert [button.text for row in delegated_edit.reply_markup.rows for button in row.buttons][-1] == "← Back to setup"
    back_data = next(
        button.type.data.decode() if isinstance(button.type.data, bytes) else button.type.data
        for row in delegated_edit.reply_markup.rows for button in row.buttons
        if button.text == "← Back to setup"
    )

    assert await adapter._handle_control_callback(callback, source, back_data)
    home_edit = adapter._client.await_args.args[0]
    assert isinstance(home_edit, functions.messages.EditMessageRequest)
    assert home_edit.id == 501
    assert "Hermes setup" in home_edit.message
    assert callback.answer.await_count >= 1
    current = next(iter(setup._states.values()))
    assert await adapter._handle_control_callback(callback, source, f"hm:setup:{current['id']}:section:terminal")
    terminal = adapter._client.await_args.args[0]
    labels = [button.text for row in terminal.reply_markup.rows for button in row.buttons]
    assert labels.count("← Back to setup") == 1
    payloads = [button.type.data.decode() for row in terminal.reply_markup.rows for button in row.buttons]
    assert not any(payload.startswith("hm:setup-back:") for payload in payloads)


@pytest.mark.parametrize("surface", ["settings", "providers", "skills", "logs", "model"])
@pytest.mark.asyncio
async def test_real_delegated_surfaces_keep_card_and_return_to_setup(tmp_path, surface):
    adapter = TelethonAdapter(PlatformConfig())
    home = tmp_path / f"profile-{surface}"
    home.mkdir()
    source = SimpleNamespace(
        chat_type="dm", chat_id="101", user_id="101", user_name="Owner",
        profile="alpha", thread_id="77",
    )
    adapter._settings_home = lambda _source: home
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: home)
    adapter._peer_for = lambda _chat_id: "peer-101"
    adapter._authorized = lambda _source: True
    adapter._is_owner_user = lambda user_id, source=None: str(user_id) == "101"
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=601))
    adapter._control_route_store = {}
    setup = adapter._telegram_setup_ui = TelegramSetupUI(adapter)
    await setup.handle_command(source)
    route = adapter._setup_route_for_card(source, 601)
    assert route is not None

    from plugins.platforms.telegram.control_center import _CONTROL_TARGET
    target = {"source": source, "message_id": "601", "return_route": route}
    scope = _CONTROL_TARGET.set(target)
    try:
        if surface == "settings":
            await adapter._settings_ui().open(source)
        elif surface == "providers":
            await adapter._provider_ui().open(
                source,
                [{"slug": "openrouter", "name": "OpenRouter", "models": ["model-a"]}],
                current_model="model-a", current_provider="openrouter",
                session_key="session-a", profile_home=home,
            )
        elif surface == "skills":
            await adapter._skills_ui().open(source)
        elif surface == "logs":
            await adapter._diagnostics_ui().open(source, "logs")
        else:
            await adapter._model_settings_ui().open_settings(source, {}, "session-a")
    finally:
        _CONTROL_TARGET.reset(scope)

    delegated_edit = adapter._client.await_args.args[0]
    assert isinstance(delegated_edit, functions.messages.EditMessageRequest)
    assert delegated_edit.id == 601
    back_data = next(
        button.type.data.decode() if isinstance(button.type.data, bytes) else button.type.data
        for row in delegated_edit.reply_markup.rows for button in row.buttons
        if button.text == "← Back to setup"
    )
    callback = SimpleNamespace(message_id=601, answer=AsyncMock())
    assert await adapter._handle_control_callback(callback, source, back_data)
    home_edit = adapter._client.await_args.args[0]
    assert isinstance(home_edit, functions.messages.EditMessageRequest)
    assert home_edit.id == 601
    assert "Hermes setup" in home_edit.message


@pytest.mark.asyncio
async def test_real_settings_typed_input_refreshes_origin_keyboard_with_setup_back(tmp_path):
    adapter = TelethonAdapter(PlatformConfig())
    home = tmp_path / "profile-input"
    home.mkdir()
    source = SimpleNamespace(
        chat_type="dm", chat_id="101", user_id="101", user_name="Owner",
        profile="alpha", thread_id="77",
    )
    adapter._settings_home = lambda _source: home
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: home)
    adapter._peer_for = lambda _chat_id: "peer-101"
    adapter._authorized = lambda _source: True
    adapter._is_owner_user = lambda user_id, source=None: str(user_id) == "101"
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=701))
    adapter._control_route_store = {}
    setup = adapter._telegram_setup_ui = TelegramSetupUI(adapter)
    await setup.handle_command(source)

    callback = SimpleNamespace(message_id=701, answer=AsyncMock())
    await adapter._dispatch_control_command(source, "settings", reply_to=701, event=callback)
    settings_ui = adapter._settings_ui()
    state = next(iter(settings_ui._states.values()))
    edit_action = f"ps:{state['id']}:edit-general"
    await adapter._invoke_control_ui_callback(callback, source, settings_ui.handle_callback, edit_action)

    message_event = SimpleNamespace(message=SimpleNamespace(date=None, reply_to=None))
    assert await adapter._invoke_control_ui_message(
        message_event, source, settings_ui, settings_ui.handle_message,
        "Keep my responses concise.",
    )
    refreshed = adapter._client.await_args.args[0]
    assert isinstance(refreshed, functions.messages.EditMessageRequest)
    assert refreshed.id == 701
    labels = [button.text for row in refreshed.reply_markup.rows for button in row.buttons]
    assert "← Back to setup" in labels


def test_route_identity_fails_closed_and_does_not_fallback_from_wrong_thread(routed_adapter):
    adapter, source, setup_state = routed_adapter
    home = Path(setup_state["home"])
    # A route can never become globally addressable when the profile resolver fails.
    adapter._settings_home = lambda _source: (_ for _ in ()).throw(RuntimeError("unavailable"))
    assert adapter._target_for_callback(SimpleNamespace(message_id=77), source) is None
    assert adapter._control_route_store == {}

    adapter._settings_home = lambda _source: home
    adapter._control_route_store = {}
    # Establish an exact route, then prove a caller presenting a different non-empty
    # topic ID cannot recover it through the omitted-thread fallback.
    source_thread = SimpleNamespace(**vars(source))
    key = adapter._route_identity(source_thread, 77)
    adapter._control_route_store[key] = {
        "created": time.monotonic(), "setup_id": 9, "source": source_thread,
        "home": str(home), "profile": "alpha",
        "user_id": "101", "chat_id": "101", "thread_id": "77", "message_id": "77",
    }
    wrong_thread = SimpleNamespace(**{**vars(source), "thread_id": "88"})
    assert adapter._target_for_callback(SimpleNamespace(message_id=77), wrong_thread) is None


def test_control_routes_are_adapter_local(routed_adapter):
    adapter, source, _setup_state = routed_adapter
    identity = adapter._route_identity(source, 77)
    adapter._control_route_store[identity] = {"created": time.monotonic()}
    other = TelethonAdapter(PlatformConfig())
    assert other._control_routes() == {}
    assert adapter._control_routes() == {identity: adapter._control_route_store[identity]}


@pytest.mark.asyncio
async def test_standalone_back_is_setup_on_same_card_not_two_button_home(routed_adapter):
    adapter, source, _setup_state = routed_adapter
    ui = TelegramStandaloneUI(adapter)
    state = {"id": 2, "created": time.monotonic(),
             "home": str(adapter._settings_home(source)),
             "profile": "alpha", "user_id": "101", "chat_id": "101",
             "thread_id": "77", "message_id": "77", "view": "browser"}
    text, markup = ui._home_view(state)
    assert "Hermes setup" in text and "Hermes controls" not in text
    labels = [button.text for row in markup.rows for button in row.buttons]
    assert labels == ["Open setup"]

    ui._states[2] = dict(state)
    ui._allowed_action = lambda _state, action: action == "setup"
    callback = SimpleNamespace(message_id=77, answer=AsyncMock())
    adapter._dispatch_control_command = AsyncMock()
    assert await ui._handle_callback_scoped(
        callback, source, "hm:standalone:2:setup",
    )
    adapter._dispatch_control_command.assert_awaited_once_with(
        source, "setup", reply_to="77", event=callback,
    )
