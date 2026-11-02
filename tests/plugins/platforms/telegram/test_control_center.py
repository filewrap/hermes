"""Control-centre routes stay native, owner-bound, and consistent with Telegram menus."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.fixture
def control_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda _home: False)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm", user_id="100")
    adapter = TelethonAdapter(PlatformConfig())
    adapter._source = AsyncMock(return_value=source)
    adapter._multiuser_home = lambda _source: None
    adapter._authorized = lambda _source: True
    adapter._is_owner_user = lambda uid, source=None: uid == "100"
    adapter._group_triggered = AsyncMock(return_value=True)
    adapter._refresh_command_menu = AsyncMock()
    adapter._cache_inbound_peer = AsyncMock()
    adapter._send_rich_ui = AsyncMock()
    adapter._prepare_attachment_intake = AsyncMock(return_value=None)
    adapter._telegram_setup_ui = SimpleNamespace(handle_command=AsyncMock(), handle_message=AsyncMock(return_value=False), handle_callback=AsyncMock())
    adapter._telegram_standalone_ui = SimpleNamespace(handle_command=AsyncMock(), handle_callback=AsyncMock())
    adapter._telegram_thread_ui = SimpleNamespace(handle_command=AsyncMock(), handle_callback=AsyncMock())
    adapter._telegram_web_auth_ui = SimpleNamespace(handle_command=AsyncMock(), handle_message=AsyncMock(return_value=False), handle_callback=AsyncMock())
    for factory in (adapter._model_settings_ui, adapter._provider_ui, adapter._settings_ui, adapter._skills_ui):
        factory().handle_message = AsyncMock(return_value=False)
    adapter.handle_message = AsyncMock()
    return adapter, source


@pytest.mark.asyncio
@pytest.mark.parametrize("command,controller", [("setup", "setup"), ("browser", "standalone"), ("dashboard", "standalone"), ("webui", "standalone"), ("webauth", "web_auth"), ("thread", "thread")])
async def test_inbound_commands_route_to_native_controller_not_agent(control_transport, command, controller):
    adapter, source = control_transport
    await adapter._on_new_message(SimpleNamespace(message=SimpleNamespace(
        message=f"/{command} status", id=55, reply_to_msg_id=None,
    )))
    ui = getattr(adapter, f"_telegram_{controller}_ui")
    ui.handle_command.assert_awaited_once()
    args = ui.handle_command.await_args.args
    assert args[0] is source
    assert args[-1] == "status"
    assert ui.handle_command.await_args.kwargs["reply_to"] == 55
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix,controller", [("hm:setup:", "setup"), ("hm:standalone:", "standalone"), ("hm:thread:", "thread"), ("hm:webauth:", "web_auth")])
async def test_callback_route_rechecks_owner_before_controller(control_transport, prefix, controller):
    adapter, source = control_transport
    callback = SimpleNamespace(data=(prefix + "token:home").encode(), message_id=8, answer=AsyncMock())
    await adapter._on_callback_query(callback)
    ui = getattr(adapter, f"_telegram_{controller}_ui")
    ui.handle_callback.assert_awaited_once_with(callback, source, prefix + "token:home")
    ui.handle_callback.reset_mock()
    source.user_id = "200"
    await adapter._on_callback_query(callback)
    ui.handle_callback.assert_not_awaited()
    assert "owner" in callback.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_hub_model_delegation_uses_existing_command_event(control_transport):
    adapter, source = control_transport
    await adapter._dispatch_control_command(source, "model", reply_to=55)
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.get_command() == "model"
    assert event.source is source
    assert event.message_id == "55"
    adapter.handle_message.reset_mock()
    source.user_id = "200"
    await adapter._dispatch_control_command(source, "model")
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_rich_ui_receipt_retains_native_html_and_thread_target():
    adapter = TelethonAdapter(PlatformConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm", user_id="100", thread_id="77")
    receipt = SimpleNamespace(id=98)
    adapter._client = AsyncMock(return_value=receipt)
    adapter._peer_for = lambda _chat: types.InputPeerUser(100, 0)
    result = await adapter._send_rich_ui(source, "<h1>Control centre</h1><p><i>Page 1 of 1 • Total 1</i></p>", reply_to=55)
    assert result is receipt
    request = adapter._client.await_args.args[0]
    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    assert request.reply_to.top_msg_id == 77
    assert request.reply_to.reply_to_msg_id == 55


def test_help_and_native_menu_expose_same_adapter_commands(monkeypatch):
    from hermes_cli.commands_platforms import TELEGRAM_CONTROL_COMMANDS, telegram_bot_commands
    from plugins.platforms.telegram.rich_ui import help_sections
    from hermes_cli.commands import resolve_command
    menu = dict(telegram_bot_commands(include_plugins=False))
    sections = help_sections(skill_commands={})
    listed = {name.split()[0].lstrip("/") for _section, entries in sections for name, _description in entries}
    for name, _description, _args, _category in TELEGRAM_CONTROL_COMMANDS:
        assert name in menu and name in listed
    assert resolve_command("thread").gateway_only
    assert resolve_command("topic").name == "topic"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["setup", "browser", "dashboard", "webui", "webauth", "thread"])
async def test_nonowner_control_commands_never_invoke_underlying_operations(control_transport, command):
    adapter, source = control_transport
    source.user_id = "200"
    assert await adapter._handle_control_command(source, command)
    for ui in (adapter._telegram_setup_ui, adapter._telegram_standalone_ui, adapter._telegram_thread_ui, adapter._telegram_web_auth_ui):
        ui.handle_command.assert_not_awaited()
    assert "owner" in adapter._send_rich_ui.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix,expected", [("hm:thread:", True), ("hm:setup:", False), ("hm:standalone:", False), ("hm:webauth:", False)])
async def test_multiuser_group_scope_only_admits_owner_thread_controls(control_transport, monkeypatch, tmp_path, prefix, expected):
    adapter, source = control_transport
    source.chat_type = "group"
    source.chat_id = "-100123"
    adapter._multiuser_home = lambda _source: tmp_path
    monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda _home: True)
    monkeypatch.setattr("gateway.mongo_multiuser.owner_user_id", lambda _home: "100")
    callback = SimpleNamespace(data=(prefix + "token:home").encode(), message_id=8, answer=AsyncMock())
    await adapter._on_callback_query(callback)
    assert adapter._telegram_thread_ui.handle_callback.await_count == int(expected)
    adapter._telegram_setup_ui.handle_callback.assert_not_awaited()
    adapter._telegram_standalone_ui.handle_callback.assert_not_awaited()
    adapter._telegram_web_auth_ui.handle_callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_matching", [True, False])
async def test_native_card_receipt_never_borrows_an_unrelated_update_id(include_matching):
    adapter = TelethonAdapter(PlatformConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm", user_id="100")
    adapter._peer_for = lambda _chat: types.InputPeerUser(100, 0)

    async def send(request):
        updates = [types.UpdateMessageID(id=8, random_id=request.random_id + 1)]
        if include_matching:
            updates.append(types.UpdateMessageID(id=9, random_id=request.random_id))
        return SimpleNamespace(updates=updates)

    adapter._client = AsyncMock(side_effect=send)
    result = await adapter._send_rich_ui(source, "<h1>Setup</h1>")
    assert result.id == ("9" if include_matching else None)


@pytest.mark.asyncio
async def test_missing_card_id_cannot_create_an_unbound_replacement(control_transport):
    adapter, source = control_transport
    with pytest.raises(ValueError, match="originating control card"):
        await adapter._edit_rich_ui_message(SimpleNamespace(), source, "<h1>Setup</h1>")
    adapter._send_rich_ui.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_auth_input_is_consumed_before_agent_or_attachment_processing(control_transport):
    adapter, _source = control_transport
    adapter._telegram_web_auth_ui.handle_message.return_value = True
    event = SimpleNamespace(message=SimpleNamespace(message="test-password-not-a-live-secret", id=66, reply_to_msg_id=None))
    await adapter._on_new_message(event)
    adapter._telegram_web_auth_ui.handle_message.assert_awaited_once()
    adapter._telegram_setup_ui.handle_message.assert_not_awaited()
    adapter._prepare_attachment_intake.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["setup", "browser", "dashboard", "webui", "webauth"])
async def test_private_configuration_commands_show_a_visible_group_boundary(control_transport, command):
    adapter, source = control_transport
    source.chat_type = "group"
    assert await adapter._handle_control_command(source, command)
    for ui in (adapter._telegram_setup_ui, adapter._telegram_standalone_ui, adapter._telegram_web_auth_ui):
        ui.handle_command.assert_not_awaited()
    assert "private chat" in adapter._send_rich_ui.await_args.args[1]


@pytest.mark.asyncio
async def test_existing_topic_mode_commands_keep_gateway_routing(control_transport, monkeypatch):
    adapter, source = control_transport
    source.chat_type = "group"
    source.chat_id = "-100123"
    adapter._cache_inbound_media = AsyncMock(return_value=([], [], ""))
    monkeypatch.setattr("plugins.platforms.telegram.incoming_reactions.react_to_incoming", AsyncMock())
    assert await adapter._handle_control_command(source, "topic", "on") is False
    await adapter._on_new_message(SimpleNamespace(message=SimpleNamespace(message="/topic on", id=55, reply_to_msg_id=None)))
    adapter._telegram_thread_ui.handle_command.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].get_command() == "topic"
    assert adapter.handle_message.await_args.args[0].get_command_args() == "on"
