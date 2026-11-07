"""Intent reactions use native MTProto, respect user scope, and never own a turn."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import functions, types

from gateway.config import PlatformConfig
from gateway.platforms.event import ProcessingOutcome
from gateway.telegram_personal_settings import save_personal_settings
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def make_adapter(monkeypatch, home):
    monkeypatch.setenv("HERMES_HOME", str(home))
    for user_id in (7, 8):
        save_personal_settings(home, user_id, {"text_reactions_enabled": True})
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = AsyncMock()
    adapter._client.session = SimpleNamespace(process_entities=Mock())
    adapter._refresh_command_menu = AsyncMock()
    adapter._peer_for = lambda chat_id: int(chat_id)
    monkeypatch.setattr(adapter, "_authorized", lambda source: True)
    adapter.handle_message = AsyncMock()
    return adapter


def incoming(text="it worked", user=7, *, group=False, forwarded=False, reply=None):
    sender = SimpleNamespace(id=user, first_name="Alice", bot=False)
    message = SimpleNamespace(
        id=12, message=text, photo=None, video=None, voice=None, audio=None,
        sticker=None, document=None, file=None, reply_to=None,
        reply_to_msg_id=11 if reply else None,
        fwd_from=SimpleNamespace(from_id=99) if forwarded else None,
    )
    if reply:
        message.get_reply_message = AsyncMock(return_value=reply)
    chat = SimpleNamespace(id=-1009, title="Group", megagroup=True) if group else sender
    peer = types.InputPeerChannel(9, 0) if group else types.InputPeerUser(user, 0)
    return SimpleNamespace(message=message, chat_id=-1009 if group else user, sender=sender, chat=chat, input_chat=peer)


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected", [("it worked", "🎉"), ("I suggest adding reactions", "👀")])
async def test_real_inbound_native_request_preserves_message_and_reaction(monkeypatch, tmp_path, text, expected):
    adapter = make_adapter(monkeypatch, tmp_path)
    await adapter._on_new_message(incoming(text))
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == text
    request = adapter._client.await_args.args[0]
    assert isinstance(request, functions.messages.SendReactionRequest)
    assert isinstance(request.peer, types.InputPeerUser)
    assert request.peer.user_id == 7 and request.msg_id == 12
    assert bytes(request)
    assert [r.emoticon for r in request.reaction] == [expected]
    # Processing outcome must not replace the user's intent with a task-status emoji.
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
    assert adapter._client.await_count == 1


@pytest.mark.asyncio
async def test_opt_out_persists_across_instances_users_groups_and_profile_routes(monkeypatch, tmp_path):
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    save_personal_settings(home_a, 7, {"reactions_enabled": False, "text_reactions_enabled": True})
    adapter = make_adapter(monkeypatch, home_a)
    adapter._group_triggered = AsyncMock(return_value=True)
    for group in (False, True):
        await adapter._on_new_message(incoming(group=group))
    adapter._client.assert_not_awaited()
    await adapter._on_new_message(incoming(user=8))
    assert adapter._client.await_count == 1
    # A route's home, not process HERMES_HOME, determines this user's opt-out.
    save_personal_settings(home_b, 7, {"reactions_enabled": True, "text_reactions_enabled": True})
    active = [home_b]
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda source: active[0])
    await adapter._on_new_message(incoming())
    assert adapter._client.await_count == 2
    active[0] = home_a
    await adapter._on_new_message(incoming())
    assert adapter._client.await_count == 2
    fresh = make_adapter(monkeypatch, home_a)
    await fresh._on_new_message(incoming())
    fresh._client.assert_not_awaited()
    save_personal_settings(home_a, 7, {"reactions_enabled": True, "text_reactions_enabled": True})
    await fresh._on_new_message(incoming())
    fresh._client.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_author,expected_count", [(99, 1), (88, 0)])
async def test_only_existing_bot_reply_context_can_resolve_short_confirmation(
    monkeypatch, tmp_path, reply_author, expected_count,
):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._bot_id = 99
    reply = SimpleNamespace(message="Did it work?", sender_id=reply_author)
    event = incoming("yes", reply=reply)
    await adapter._on_new_message(event)
    event.message.get_reply_message.assert_awaited_once()
    assert adapter._client.await_count == expected_count
    if expected_count:
        request = adapter._client.await_args.args[0]
        assert request.reaction[0].emoticon == "🎉"
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unauthorized", "untriggered", "command", "forwarded", "unknown", "bot"])
async def test_unadmitted_or_unsuitable_messages_never_react(monkeypatch, tmp_path, case):
    adapter = make_adapter(monkeypatch, tmp_path)
    event = incoming("/status" if case == "command" else "abcxyz" if case == "unknown" else "it worked",
                     group=case == "untriggered", forwarded=case == "forwarded")
    if case == "unauthorized":
        monkeypatch.setattr(adapter, "_authorized", lambda source: False)
    if case == "untriggered":
        adapter._group_triggered = AsyncMock(return_value=False)
    if case == "bot":
        event.sender.bot = True
    await adapter._on_new_message(event)
    adapter._client.assert_not_awaited()
    # Bot traffic is isolated in the supervised peer inbox, never a human model turn.
    assert adapter.handle_message.await_count == (0 if case in {"unauthorized", "untriggered", "bot"} else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["telegram", "timeout", "settings"])
async def test_cosmetic_failure_does_not_stop_normal_message(monkeypatch, tmp_path, failure):
    from plugins.platforms.telegram import incoming_reactions
    adapter = make_adapter(monkeypatch, tmp_path)
    if failure == "telegram":
        adapter._client.side_effect = RuntimeError("restricted reaction")
    elif failure == "timeout":
        import asyncio
        async def slow(*args):
            await asyncio.sleep(10)
        adapter.send_reaction = slow
        monkeypatch.setattr(incoming_reactions, "_REACTION_TIMEOUT_SECONDS", 0.01)
    else:
        monkeypatch.setattr(incoming_reactions, "load_personal_settings", lambda *args: (_ for _ in ()).throw(OSError("unreadable")))
    await adapter._on_new_message(incoming())
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].text == "it worked"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["?", "lol", "thanks", "it worked"])
async def test_text_reactions_are_optional_not_default_keyword_autopilot(monkeypatch, tmp_path, text):
    adapter = make_adapter(monkeypatch, tmp_path)
    save_personal_settings(tmp_path, 7, {"text_reactions_enabled": False})
    await adapter._on_new_message(incoming(text))
    adapter._client.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_reaction_request_uses_native_reaction_not_model_turn(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    await adapter._on_new_message(incoming("React to this message 🔥", reply=SimpleNamespace(
        message="A message to react to", sender_id=99,
    )))

    adapter.handle_message.assert_not_awaited()
    adapter._client.assert_awaited_once()
    request = adapter._client.await_args.args[0]
    assert isinstance(request, functions.messages.SendReactionRequest)
    assert request.msg_id == 11
    assert request.reaction[0].emoticon == "🔥"


@pytest.mark.asyncio
async def test_explicit_reaction_request_is_not_executed_for_unauthorized_sender(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(adapter, "_authorized", lambda _source: False)

    await adapter._on_new_message(incoming("React to this message 👍"))

    adapter._client.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
