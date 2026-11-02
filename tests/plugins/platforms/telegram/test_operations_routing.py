"""Exercise native operation routing without Telegram connections or real sends."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.fixture
def transport(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100,200\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": False}))
    adapter._bot_id = 900
    adapter._bot_username = "HermesPrivateBot"
    adapter._client = SimpleNamespace()
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda source: tmp_path)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-10042", chat_type="group",
                           user_id="100", thread_id="77", message_id="12")
    adapter._source = AsyncMock(return_value=source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter._refresh_command_menu = AsyncMock()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerChannel(42, 123))
    adapter._mtproto_rich_request = AsyncMock(return_value=SimpleNamespace(id=800))
    adapter.send = AsyncMock()
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.incoming_reactions.react_to_incoming", AsyncMock())
    return adapter, source


def event(text, message_id=12):
    return SimpleNamespace(message=SimpleNamespace(message=text, id=message_id, reply_to_msg_id=None))


@pytest.mark.asyncio
async def test_native_read_enters_real_operation_ui_not_model(transport):
    adapter, source = transport
    await adapter._on_new_message(event("/tgmessages history"))
    adapter.handle_message.assert_not_awaited()
    operations = adapter._client_operations_ui().operations
    messages = operations._observed_messages(source)
    assert any(m.id == 12 for m in messages)
    adapter._mtproto_rich_request.assert_awaited()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.rich_message is not None


@pytest.mark.asyncio
async def test_nonowner_mutation_never_reaches_operation_rpc_or_model(transport):
    adapter, source = transport
    source.user_id = "200"
    await adapter._on_new_message(event("/poll create choose | first | second"))
    adapter.handle_message.assert_not_awaited()
    adapter._mtproto_rich_request.assert_not_awaited()
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_unlisted_bot_never_becomes_human_owner_or_tenant_turn(transport):
    adapter, source = transport
    source.is_bot = True
    source.user_id = "333"
    await adapter._on_new_message(event("/tgadmin delete 12"))
    adapter.handle_message.assert_not_awaited()
    adapter._mtproto_rich_request.assert_not_awaited()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_bot_command_address_never_enters_operation_ui(transport):
    adapter, source = transport
    await adapter._on_new_message(event("/poll@AnotherBot create Q | A | B"))
    adapter.handle_message.assert_not_awaited()
    adapter._mtproto_rich_request.assert_not_awaited()
    assert adapter._client_operations_ui().operations._observed_messages(source) == []


@pytest.mark.asyncio
async def test_managed_update_home_is_transport_home_not_child_creator_route(transport, tmp_path):
    adapter, _source = transport
    operations = SimpleNamespace(handle_managed_update=AsyncMock())
    adapter._telegram_bot_operations_ui = SimpleNamespace(operations=operations)
    update = types.UpdateManagedBot(user_id=200, bot_id=333, qts=1)
    await adapter._on_managed_bot_update(update)
    operations.handle_managed_update.assert_awaited_once_with(update, profile_home=tmp_path)


@pytest.mark.asyncio
async def test_self_bot_profile_actions_never_route_to_bot_peer_namespace(transport):
    adapter, source = transport
    read = await adapter.request_telegram_operation(source, "bot_profile", {})
    assert read["ok"]
    assert isinstance(adapter._mtproto_rich_request.await_args.args[0], functions.bots.GetBotInfoRequest)
    write = await adapter.request_telegram_operation(source, "bot_profile_update", {"about": "Exact bot about"})
    assert write["status"] == "confirmation_required"
    assert getattr(adapter, "_telegram_bot_operations_ui", None) is None
    assert not any(isinstance(call.args[0], functions.bots.SetBotInfoRequest)
                   for call in adapter._mtproto_rich_request.await_args_list)


@pytest.mark.asyncio
async def test_capabilities_have_working_parameter_catalogs_and_no_writes(transport):
    adapter, source = transport
    result = await adapter.request_telegram_operation(source, "capabilities", {})
    assert result["success"]
    assert result["client"]["data"]["parameters"]["poll_create"]["question"]
    assert "rich_message_send" in result["client"]["data"]["direct_actions"]
    assert result["client"]["data"]["parameters"]["rich_message_send"]["buttons"]
    assert result["bots"]["parameters"]["peer.send"]["text"]
    assert "managed.access.set" in result["bots"]["actions"]
    assert "managed.set_info" not in result["bots"]["actions"]
    assert result["bots"]["token_export_exposed"] is False
    adapter._mtproto_rich_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_poll_updates_refresh_only_already_observed_profile_scoped_poll(transport):
    adapter, source = transport
    poll = types.Poll(id=77, hash=0, question=types.TextWithEntities(text="Q", entities=[]),
                     answers=[types.PollAnswer(text=types.TextWithEntities(text="A", entities=[]), option=b"a")])
    message = SimpleNamespace(id=88, media=types.MessageMediaPoll(poll=poll,
                               results=types.PollResults(total_voters=0)))
    adapter._observe_telegram_message(source, message)
    # Native UpdateMessagePoll may omit msg_id. Poll ID must still match known state.
    await adapter._on_poll_update(types.UpdateMessagePoll(poll_id=77,
        results=types.PollResults(total_voters=9)))
    assert message.media.results.total_voters == 9
    await adapter._on_poll_update(types.UpdateMessagePoll(poll_id=999,
        results=types.PollResults(total_voters=50)))
    assert message.media.results.total_voters == 9
    adapter._mtproto_rich_request.assert_not_awaited()
