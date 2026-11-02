"""End-to-end contracts for Telegram reaction preferences and native delivery."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import functions, types

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.telegram_personal_settings import save_personal_settings
from plugins.platforms.telegram.incoming_reactions import (
    react_to_incoming,
    react_to_telethon_incoming,
)
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _source(user_id="7", chat_id="7", *, chat_type="dm", is_bot=False):
    return SimpleNamespace(
        user_id=user_id, user_name=user_id, chat_id=chat_id, chat_type=chat_type,
        is_bot=is_bot, thread_id=None,
    )


def _message(text="it worked", *, message_id=12, forwarded=False, media=False):
    return SimpleNamespace(
        id=message_id, message=text,
        fwd_from=SimpleNamespace(from_id=88) if forwarded else None,
        photo=object() if media else None, video=None, voice=None, audio=None,
        sticker=None, document=None, media=None,
    )


def _normalized(text="it worked", *, source=None, message_id="12", kind=MessageType.TEXT):
    return MessageEvent(
        text=text, message_type=kind, source=source or _source(),
        message_id=message_id, raw_message=_message(text, message_id=int(message_id or 0)),
    )


def _adapter(monkeypatch, home):
    monkeypatch.setenv("HERMES_HOME", str(home))
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = AsyncMock()
    adapter._client.session = SimpleNamespace(process_entities=Mock())
    adapter._refresh_command_menu = AsyncMock()
    # A native TL peer makes the request serializable, not just mock-inspectable.
    adapter._peer_for = lambda _chat_id: types.InputPeerSelf()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerSelf())
    return adapter


@pytest.mark.asyncio
async def test_personal_preference_is_reloaded_from_routed_profile_and_native_request_serializes(
    monkeypatch, tmp_path,
):
    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    save_personal_settings(home_a, "7", {"reactions_enabled": False, "text_reactions_enabled": True})
    save_personal_settings(home_b, "7", {"reactions_enabled": True, "text_reactions_enabled": True})
    active_home = [home_a]
    adapter = _adapter(monkeypatch, tmp_path / "process-default")
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: active_home[0],
    )
    event = _normalized()

    # OFF in the routed profile means no transport call, despite the default
    # profile and another profile having the same user's setting enabled.
    assert await react_to_incoming(adapter, event) is False
    adapter._client.assert_not_awaited()

    # The same adapter reloads settings per incoming message; no cached toggle.
    active_home[0] = home_b
    assert await react_to_incoming(adapter, event) is True
    request = adapter._client.await_args.args[0]
    assert isinstance(request, functions.messages.SendReactionRequest)
    assert request.msg_id == 12
    assert isinstance(request.reaction[0], types.ReactionEmoji)
    assert request.reaction[0].emoticon == "🎉"
    encoded = bytes(request)
    assert encoded and isinstance(encoded, bytes)

    active_home[0] = home_a
    save_personal_settings(home_a, "7", {"reactions_enabled": True, "text_reactions_enabled": True})
    assert await react_to_incoming(adapter, event) is True
    assert adapter._client.await_count == 2


@pytest.mark.asyncio
async def test_raw_telethon_events_require_admission_and_use_same_profile_setting(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path)
    save_personal_settings(tmp_path, "7", {"reactions_enabled": True, "text_reactions_enabled": True})
    source = _source(chat_id="-1009", chat_type="group")
    raw = SimpleNamespace(message=_message(), chat_id=-1009)
    assert await react_to_incoming(adapter, raw, source=source) is False
    adapter._client.assert_not_awaited()

    assert await react_to_telethon_incoming(adapter, raw, source) is True
    request = adapter._client.await_args.args[0]
    assert isinstance(request, functions.messages.SendReactionRequest)
    assert request.msg_id == 12
    assert request.reaction[0].emoticon == "🎉"


@pytest.mark.asyncio
async def test_raw_telethon_reaction_uses_only_direct_reply_to_our_bot(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path)
    adapter._bot_id = 99
    save_personal_settings(tmp_path, "7", {"reactions_enabled": True, "text_reactions_enabled": True})
    source = _source()

    async def event_with_reply(sender_id):
        message = _message("yes", message_id=13)
        message.reply_to_msg_id = 12
        return SimpleNamespace(
            chat_id="7",
            message=message,
            get_reply_message=AsyncMock(return_value=SimpleNamespace(
                sender_id=sender_id, message="Did it work?", id=12,
            )),
        )

    # A reply to the bot carries the same bounded context used by normalized
    # MessageEvents, so a terse success confirmation can be interpreted.
    own_reply = await event_with_reply(99)
    assert await react_to_telethon_incoming(adapter, own_reply, source) is True
    request = adapter._client.await_args.args[0]
    assert request.msg_id == 13
    assert request.reaction[0].emoticon == "🎉"
    own_reply.get_reply_message.assert_awaited_once_with()

    # Text from another participant must not be used as the bot's conversational
    # context; an ambiguous "yes" therefore produces no reaction.
    other_reply = await event_with_reply(7)
    assert await react_to_telethon_incoming(adapter, other_reply, source) is False
    assert adapter._client.await_count == 1
    other_reply.get_reply_message.assert_awaited_once_with()

    unrelated = await event_with_reply(99)
    unrelated.message.reply_to_msg_id = None
    assert await react_to_telethon_incoming(adapter, unrelated, source) is False
    assert adapter._client.await_count == 1
    unrelated.get_reply_message.assert_not_awaited()

    mismatched = await event_with_reply(99)
    mismatched.message.reply_to_msg_id = 14
    assert await react_to_telethon_incoming(adapter, mismatched, source) is False
    assert adapter._client.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event,source,admitted",
    [
        (_normalized("/help"), _source(), None),
        (_normalized("it worked", source=_source(is_bot=True)), None, None),
        (_normalized("it worked", source=_source(chat_type="channel")), None, None),
        (_normalized("it worked", kind=MessageType.COMMAND), None, None),
        (_normalized("it worked", message_id=None), None, None),
    ],
)
async def test_nontext_commands_bot_channel_or_missing_identity_never_deliver(
    monkeypatch, tmp_path, event, source, admitted,
):
    adapter = _adapter(monkeypatch, tmp_path)
    assert await react_to_incoming(adapter, event, source=source, admitted=admitted) is False
    adapter._client.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_forwarded_media_and_unadmitted_events_never_deliver(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path)
    source = _source(chat_type="group", chat_id="-1009")
    for message in (_message(forwarded=True), _message(media=True)):
        event = SimpleNamespace(message=message)
        assert await react_to_incoming(adapter, event, source=source, admitted=True) is False
    adapter._client.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_transport_is_observable_without_logging_message_content(caplog, tmp_path):
    save_personal_settings(tmp_path, "7", {"reactions_enabled": True, "text_reactions_enabled": True})

    class FailingAdapter:
        def _settings_ui(self):
            return SimpleNamespace(_home=lambda _source: tmp_path)

        async def send_reaction(self, *_args):
            raise RuntimeError("private message contents must not appear")

    assert await react_to_incoming(FailingAdapter(), _normalized()) is False
    assert "delivery failed (RuntimeError)" in caplog.text
    assert "private message contents" not in caplog.text


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path):
    """Use real local tenant authorization/settings stores and mocked IO edges."""
    from gateway.mongo_multiuser import store_for_home

    homes = {name: tmp_path / name for name in ("profile-a", "profile-b")}
    for home in homes.values():
        home.mkdir()
        (home / ".env").write_text(
            "OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=200\n",
            encoding="utf-8",
        )
        store_for_home(home)
    active_home = [homes["profile-a"]]
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": True}))
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: active_home[0],
    )
    adapter._source = AsyncMock(side_effect=lambda event: event.source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter.send = AsyncMock()
    adapter._client = AsyncMock()
    adapter._client.get_input_entity = AsyncMock(return_value=types.InputPeerSelf())
    adapter._bot_username = "HermesBot"
    adapter._refresh_command_menu = AsyncMock()
    return SimpleNamespace(adapter=adapter, homes=homes, active_home=active_home)


def _isolated_update(source, text="it worked"):
    return SimpleNamespace(
        source=source,
        message=SimpleNamespace(
            id=12, message=text, reply_to_msg_id=None, fwd_from=None,
            photo=None, video=None, voice=None, audio=None, sticker=None,
            document=None, media=None,
        ),
    )


@pytest.mark.asyncio
async def test_adapter_isolated_runtime_reloads_local_preference_a_b_a_before_model(
    isolated_runtime, monkeypatch,
):
    from gateway.telegram_personal_settings import save_personal_settings

    runtime = isolated_runtime
    adapter = runtime.adapter
    source = _source(user_id="200", chat_id="200", chat_type="dm")
    model_order = []
    client_order = []
    timeline = []

    async def send_native(request):
        client_order.append("reaction")
        timeline.append("reaction")
        assert isinstance(request, functions.messages.SendReactionRequest)
        assert request.reaction[0].emoticon == "🎉"
        assert bytes(request)

    adapter._client.side_effect = send_native

    async def tenant(*_args, **_kwargs):
        model_order.append("model")
        timeline.append("model")
        return {"final_response": "answer"}

    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant)

    home_a, home_b = runtime.homes["profile-a"], runtime.homes["profile-b"]
    save_personal_settings(home_a, "200", {"reactions_enabled": False, "text_reactions_enabled": True})
    save_personal_settings(home_b, "200", {"reactions_enabled": True, "text_reactions_enabled": True})

    # A (OFF) -> B (ON) -> A (OFF) uses each routed profile immediately; model
    # dispatch still runs each turn, with the one native reaction before it.
    for home, should_react in ((home_a, False), (home_b, True), (home_a, False)):
        runtime.active_home[0] = home
        source.chat_id = "200"
        before_reactions = client_order.count("reaction")
        before_models = len(model_order)
        before_timeline = len(timeline)
        await adapter._on_new_message(_isolated_update(source))
        assert len(model_order) == before_models + 1
        assert client_order.count("reaction") == before_reactions + int(should_react)
        assert timeline[before_timeline:] == (["reaction", "model"] if should_react else ["model"])

    # A user toggle is loaded again on the very next incoming raw Telethon event.
    save_personal_settings(home_a, "200", {"reactions_enabled": True, "text_reactions_enabled": True})
    before_reactions = client_order.count("reaction")
    await adapter._on_new_message(_isolated_update(source))
    assert client_order.count("reaction") == before_reactions + 1
    assert len(model_order) == 4


@pytest.mark.asyncio
async def test_adapter_rejects_unauthorized_banned_and_untriggered_name_updates(
    isolated_runtime, monkeypatch,
):
    from agent.mongo_tenants import Principal
    from gateway.mongo_multiuser import store_for_home
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings
    from gateway.telegram_personal_settings import save_personal_settings

    runtime = isolated_runtime
    adapter = runtime.adapter
    home = runtime.homes["profile-a"]
    store = store_for_home(home)
    save_personal_settings(home, "200", {"reactions_enabled": True, "text_reactions_enabled": True})
    model = AsyncMock(return_value={"final_response": "answer"})
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", model)

    # Unknown local actor: rejected by the real profile-local access store.
    await adapter._on_new_message(_isolated_update(_source("201", chat_id="201", chat_type="dm")))
    adapter._client.assert_awaited_once()
    rejection = adapter._client.await_args.args[0]
    assert isinstance(rejection, functions.messages.SendReactionRequest)
    assert rejection.reaction[0].emoticon == "🥴"
    assert store.get_access("201")["allowed"] is False
    adapter._client.reset_mock()
    model.assert_not_awaited()

    # Explicitly banned allowlisted user: deny wins over the allowlist.
    store.ban_user(Principal("100"), "200", True)
    await adapter._on_new_message(_isolated_update(_source("200", chat_id="200", chat_type="dm")))
    adapter._client.assert_not_awaited()
    model.assert_not_awaited()
    store.ban_user(Principal("100"), "200", False)

    # An allowed user still has to invoke the selected trigger. Turning guest
    # access off does not revoke their grant, but unrelated text is not a trigger.
    group_id = "-100123"
    update_chat_settings(home, "group", group_id, {"trigger_mode": "trigger", "allow_all": False})
    adapter._bot_name = "Hermes"
    group_source = _source("200", chat_id=group_id, chat_type="group")
    await adapter._on_new_message(_isolated_update(group_source, "It worked"))
    adapter._client.assert_not_awaited()
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_reacts_before_model_for_triggered_isolated_group(isolated_runtime, monkeypatch):
    from gateway.telegram_personal_settings import save_personal_settings

    runtime = isolated_runtime
    adapter = runtime.adapter
    home = runtime.homes["profile-a"]
    save_personal_settings(home, "200", {"reactions_enabled": True, "text_reactions_enabled": True})
    source = _source(user_id="200", chat_id="-100123", chat_type="group")
    timeline = []

    async def send_native(request):
        timeline.append("reaction")
        assert isinstance(request, functions.messages.SendReactionRequest)
        assert request.reaction[0].emoticon == "🎉"

    async def tenant(*_args, **_kwargs):
        timeline.append("model")
        return {"final_response": "answer"}

    adapter._client.side_effect = send_native
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant)
    await adapter._on_new_message(_isolated_update(source, "@HermesBot it worked"))

    assert timeline == ["reaction", "model"]
