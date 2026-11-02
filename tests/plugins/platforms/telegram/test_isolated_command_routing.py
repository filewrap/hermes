"""Telegram commands are controls, never isolated conversational prompts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.fixture
def transport(tmp_path, monkeypatch):
    # Exercise the real local admission store, not the retired Mongo rollout flag.
    (tmp_path / ".env").write_text(
        "OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100,200\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": True}))
    adapter._bot_username = "NainaAssistantBot"
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda source: tmp_path
    )
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-100123",
                           chat_type="group", user_id="100", user_name="Owner",
                           thread_id="77")
    adapter._source = AsyncMock(return_value=source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter.send = AsyncMock()
    adapter.handle_message = AsyncMock()
    for factory in (adapter._provider_ui, adapter._settings_ui, adapter._skills_ui):
        monkeypatch.setattr(factory(), "handle_message", AsyncMock(return_value=False))
    tenant = AsyncMock(return_value={"final_response": "isolated answer"})
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant)
    monkeypatch.setattr("plugins.platforms.telegram.incoming_reactions.react_to_incoming", AsyncMock())
    return adapter, source, tenant


def update(text):
    return SimpleNamespace(message=SimpleNamespace(message=text, id=55, reply_to_msg_id=None))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/sethome", "/sethome@nainaassistantbot", "/SETHOME@NainaAssistantBot"])
async def test_owner_group_sethome_reaches_native_runner_without_model(transport, text):
    adapter, source, tenant = transport
    await adapter._on_new_message(update(text))
    tenant.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.get_command() == "sethome"
    assert event.get_command_args() == ""
    assert event.source is source
    assert event.source.thread_id == "77"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/sethome@OtherBot", "/help@OtherBot"])
async def test_other_bot_commands_are_ignored_even_when_mentions_disabled(transport, text):
    adapter, _source, tenant = transport
    adapter.config.extra["require_mention"] = False
    await adapter._on_new_message(update(text))
    tenant.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/not_a_command@NainaAssistantBot", "/reload-skills@NainaAssistantBot"])
async def test_unknown_or_owner_only_slashes_never_enter_nonowner_tenant(transport, text):
    adapter, source, tenant = transport
    source.user_id = "200"
    await adapter._on_new_message(update(text))
    tenant.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    # A slash command is control input, never conversational tenant text.


@pytest.mark.asyncio
async def test_nonowner_sethome_is_rejected_without_entering_tenant(transport):
    adapter, source, tenant = transport
    source.user_id = "200"
    await adapter._on_new_message(update("/sethome@NainaAssistantBot"))
    tenant.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once()
    assert "Only the configured owner" in adapter.send.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", ["100", "200"])
@pytest.mark.parametrize("text", ["/settings@NainaAssistantBot", "/skills"])
async def test_settings_and_skills_are_dm_only_for_everyone(transport, uid, text):
    adapter, source, tenant = transport
    source.user_id = uid
    await adapter._on_new_message(update(text))
    tenant.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once()
    assert "private" in adapter.send.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_owner_memory_command_stays_in_owner_runtime(transport):
    adapter, _source, tenant = transport
    await adapter._on_new_message(update("/memory@NainaAssistantBot"))
    tenant.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_owner_group_plain_text_uses_owner_runtime(transport):
    adapter, source, tenant = transport
    await adapter._on_new_message(update("@NainaAssistantBot hey"))
    tenant.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].source is source


@pytest.mark.asyncio
async def test_allowed_nonowner_group_plain_text_uses_isolated_tenant(transport):
    adapter, source, tenant = transport
    source.user_id = "200"
    await adapter._on_new_message(update("@NainaAssistantBot hey"))
    tenant.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()
    assert tenant.await_args.args[2] is source
    assert adapter.send.await_args.args[1] == "isolated answer"
    assert adapter.send.await_args.kwargs["metadata"] == {"thread_id": "77"}


@pytest.mark.asyncio
async def test_group_help_is_native_not_a_model_prompt(transport, monkeypatch):
    adapter, _source, tenant = transport
    show = AsyncMock()
    monkeypatch.setattr(adapter, "_show_help", show)
    await adapter._on_new_message(update("/help@NainaAssistantBot"))
    show.assert_awaited_once()
    tenant.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_sethome_persists_chat_and_topic_not_a_filesystem_path(transport, tmp_path):
    import yaml
    from gateway.run import GatewayRunner

    adapter, source, tenant = transport
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(platforms={})

    async def dispatch(event):
        assert event.get_command() == "sethome"
        response = await runner._handle_set_home_command(event)
        await adapter.send(event.source.chat_id, response)

    adapter.handle_message = AsyncMock(side_effect=dispatch)
    await adapter._on_new_message(update("/sethome@NainaAssistantBot"))
    tenant.assert_not_awaited()
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    home = saved["platforms"]["telegram"]["home_channel"]
    assert str(home["chat_id"]) == source.chat_id
    assert str(home["thread_id"]) == source.thread_id
    assert "path" not in adapter.send.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_allowed_nonowner_registered_command_uses_tenant_dispatcher_only(transport, monkeypatch):
    adapter, source, tenant = transport
    source.user_id = "200"
    command = AsyncMock(return_value={"final_response": "tenant identity", "messages": [], "usage": {}})
    monkeypatch.setattr("gateway.tenant_commands.handle_tenant_command", command)

    await adapter._on_new_message(update("/whoami@NainaAssistantBot"))

    command.assert_awaited_once()
    assert command.await_args.args[2] is source
    assert command.await_args.kwargs["store"] is not None
    tenant.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    assert adapter.send.await_args.args[1] == "tenant identity"


@pytest.mark.asyncio
async def test_allowed_nonowner_settings_opens_personal_settings_ui(transport, monkeypatch):
    adapter, source, tenant = transport
    source.user_id = "200"
    source.chat_type = "dm"
    source.chat_id = "200"
    source.thread_id = None
    settings = adapter._settings_ui()
    skills = adapter._skills_ui()
    open_settings = AsyncMock()
    cancel_skills = MagicMock()
    monkeypatch.setattr(settings, "open", open_settings)
    monkeypatch.setattr(skills, "cancel_for_source", cancel_skills)
    command = AsyncMock(side_effect=AssertionError("settings must not reach command worker"))
    monkeypatch.setattr("gateway.tenant_commands.handle_tenant_command", command)

    await adapter._on_new_message(update("/settings@NainaAssistantBot"))

    open_settings.assert_awaited_once_with(source, reply_to=55)
    cancel_skills.assert_called_once_with(source)
    command.assert_not_awaited()
    tenant.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
