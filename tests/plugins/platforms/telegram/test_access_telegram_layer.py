"""Behaviour contract for the Telegram access layer redesign.

The only access rule is owner-or-universal-allowlist: no per-group allow-all,
no per-group access setting. Non-admitted senders are silently ignored in
groups; owner-only commands silently deny admitted non-owners. ``/trigger``
is a per-group chat setting (an invocation trigger, never an access grant),
and ``/allow`` / ``/disallow`` resolve a replied user, a username, or a
numeric ID.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from agent.mongo_tenants import Principal
from gateway.config import PlatformConfig
from gateway.mongo_multiuser import store_for_home
from plugins.platforms.telegram.telegram_chat_settings import (
    load_chat_settings,
    update_chat_settings,
)
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

OWNER = "100"
GROUP = "-100123"
OTHER_GROUP = "-100456"


def _source(user_id=OWNER, *, chat_type="group", chat_id=GROUP, is_bot=False):
    return SimpleNamespace(
        platform="telegram",
        chat_id=str(chat_id),
        chat_type=chat_type,
        user_id=str(user_id),
        user_name=f"User {user_id}",
        chat_name=f"Chat {chat_id}",
        thread_id=None,
        is_bot=is_bot,
    )


def _message(text, source):
    return SimpleNamespace(
        source=source,
        message=SimpleNamespace(message=text, id=55, reply_to_msg_id=None),
        get_sender=AsyncMock(return_value=SimpleNamespace(
            id=int(source.user_id),
            username=f"user_{source.user_id}",
            first_name=f"User {source.user_id}",
            bot=source.is_bot,
        )),
    )


def _reply_message(text, source, reply_sender_id):
    """A command message replying to ``reply_sender_id``'s message."""
    event = _message(text, source)
    event.message.reply_to_msg_id = 54
    event.message.get_reply_message = AsyncMock(return_value=SimpleNamespace(
        sender_id=reply_sender_id, message="quoted message",
    ))
    return event


def _callback(data, source, *, message_id=55):
    return SimpleNamespace(
        source=source,
        data=data.encode(),
        message_id=message_id,
        answer=AsyncMock(),
    )


def _callback_payload(markup, prefix):
    for row in getattr(markup, "rows", ()) or ():
        for button in getattr(row, "buttons", ()) or ():
            data = getattr(getattr(button, "type", None), "data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if str(data).startswith(prefix):
                return str(data)
    raise AssertionError(f"No {prefix!r} callback button in native markup")


def _confirmation_token(adapter):
    call = adapter._send_rich_ui.await_args
    assert call is not None
    payload = _callback_payload(call.args[2], "hm:allow:confirm:")
    match = re.fullmatch(r"hm:allow:confirm:([A-Za-z0-9_-]+)", payload)
    assert match, payload
    return match.group(1)


@pytest.fixture
def transport(tmp_path, monkeypatch):
    """A real local tenant store behind a mocked Telethon/UI transport."""
    (tmp_path / ".env").write_text(
        "OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=200\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": True}))
    adapter._bot_username = "NainaAssistantBot"
    adapter._bot_name = "Naina"
    adapter.send_reaction = AsyncMock(return_value=True)
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
        _profile_name_for_source=lambda _source, adapter_profile=None: adapter_profile,
    )
    adapter._source = AsyncMock(side_effect=lambda event: event.source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter._send_rich_ui = AsyncMock(return_value=SimpleNamespace(id=55))
    adapter._edit_rich_ui_message = AsyncMock()
    adapter.send = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(int(OWNER), 0))
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=901))
    adapter._client.get_entity = AsyncMock()
    adapter._client.get_input_entity = AsyncMock(
        side_effect=lambda peer: types.InputUser(int(getattr(peer, "user_id", peer)), 98765)
    )

    tenant_handler = AsyncMock(return_value={"final_response": "tenant reply"})
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant_handler)
    monkeypatch.setattr("plugins.platforms.telegram.incoming_reactions.react_to_incoming", AsyncMock())

    store = store_for_home(tmp_path)
    for user_id, username in (("200", "configuser"), ("201", "starteduser"), ("202", "adminuser"), ("203", "fourthuser")):
        store.start_user(user_id)
        store.record_started_identity(user_id, username, f"User {user_id}")
    return SimpleNamespace(
        adapter=adapter,
        store=store,
        home=tmp_path,
        tenant_handler=tenant_handler,
    )


def start_grant(store, target, scope):
    """Create a test grant through the real store API, not a mocked role."""
    store.grant_sudo(Principal(OWNER), str(target), scope=scope)


async def command(transport, text, source=None):
    source = source or _source()
    await transport.adapter._on_new_message(_message(text, source))


async def confirm(transport, token, source=None):
    source = source or _source()
    await transport.adapter._on_callback_query(
        _callback(f"hm:allow:confirm:{token}", source)
    )


# (a) /allowall no longer exists -------------------------------------------

@pytest.mark.asyncio
async def test_allowall_is_unknown_and_writes_no_access_setting(transport):
    from plugins.platforms.telegram import telegram_controls
    from plugins.platforms.telegram.telegram_access import handle_access_command

    await command(transport, "/allowall on")
    from hermes_cli.config import read_user_config_raw
    record = (read_user_config_raw(transport.home / "config.yaml")
              .get("platforms", {}).get("telegram", {}).get("chat_access", {})
              .get("groups", {}).get(GROUP, {}))
    assert "allow_all" not in record
    assert load_chat_settings(transport.home, "group", GROUP) == {
        "trigger_word": "", "trigger_mode": "", "allowed_users": {}, "blocked": False,
    }
    transport.tenant_handler.assert_not_awaited()

    source = _source()
    assert await telegram_controls.handle_owner_command(
        transport.adapter, source, "allowall", "on") is False
    event = _message("/allowall on", source)
    assert await handle_access_command(
        transport.adapter, event, source, "allowall", "on", transport.store) is False

    with pytest.raises(ValueError, match="Unsupported Telegram chat setting"):
        update_chat_settings(transport.home, "group", GROUP, {"allow_all": True})


@pytest.mark.asyncio
async def test_gsettings_is_unknown_and_writes_no_group_setting(transport):
    from plugins.platforms.telegram import telegram_controls
    from plugins.platforms.telegram.telegram_access import handle_access_command

    await command(transport, "/gsettings enabled=false")
    assert load_chat_settings(transport.home, "group", GROUP) == {
        "trigger_word": "", "trigger_mode": "", "allowed_users": {}, "blocked": False,
    }
    transport.tenant_handler.assert_not_awaited()
    transport.adapter._send_rich_ui.assert_not_awaited()

    source = _source()
    assert await telegram_controls.handle_owner_command(
        transport.adapter, source, "gsettings", "") is False
    event = _message("/gsettings", source)
    assert await handle_access_command(
        transport.adapter, event, source, "gsettings", "", transport.store) is False


@pytest.mark.asyncio
async def test_stale_allowall_callback_is_unavailable_and_changes_nothing(transport):
    start_grant(transport.store, "201", GROUP)
    callback = _callback("hm:settings:allowall:off", _source("201"))
    await transport.adapter._on_callback_query(callback)
    callback.answer.assert_awaited()
    assert "unavailable" in callback.answer.await_args.args[0].lower()
    assert load_chat_settings(transport.home, "group", GROUP)["trigger_word"] == ""


# (b) Non-admitted group members are silently ignored ------------------------

@pytest.mark.asyncio
async def test_unlisted_group_message_and_command_are_silently_ignored(transport):
    for text in ("@NainaAssistantBot hello", "/help@NainaAssistantBot"):
        await command(transport, text, _source("300"))
        transport.adapter.send.assert_not_awaited()
        transport.adapter.send_reaction.assert_not_awaited()
        transport.adapter._send_rich_ui.assert_not_awaited()
        transport.adapter.handle_message.assert_not_awaited()
        transport.tenant_handler.assert_not_awaited()
        assert transport.store.get_access("300")["allowed"] is False


# (c) Admitted group members get replies and may run commands ----------------

@pytest.mark.asyncio
async def test_allowed_group_member_reaches_tenant_turn(transport):
    start_grant(transport.store, "201", GROUP)
    await command(transport, "@NainaAssistantBot hello", _source("201"))
    transport.tenant_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_allowed_group_member_can_set_trigger_word(transport):
    start_grant(transport.store, "201", GROUP)
    await command(transport, '/trigger "word"', _source("201"))
    assert load_chat_settings(transport.home, "group", GROUP)["trigger_word"] == "word"
    transport.adapter._send_rich_ui.assert_awaited()
    text = transport.adapter._send_rich_ui.await_args.args[1]
    assert "<h1>Triggers</h1>" in text and "Group settings" not in text
    transport.tenant_handler.assert_not_awaited()


# (d) /trigger ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_trigger_word_triggers_on_word_boundary_case_insensitively(transport):
    await command(transport, '/trigger "word"', _source())
    assert load_chat_settings(transport.home, "group", GROUP)["trigger_word"] == "word"
    update_chat_settings(transport.home, "group", GROUP, {"trigger_mode": "trigger"})
    source = _source()
    for text, expected in (
        ("say word please", True),
        ("WORD", True),
        ("a word.", True),
        ("swordfish", False),
        ("say nothing", False),
        ("Naina hello", True),  # The bot-name trigger still works.
    ):
        event = _message(text, source)
        assert await transport.adapter._group_triggered(event, source) is expected


@pytest.mark.asyncio
async def test_trigger_word_alone_does_not_admit_unlisted_guest(transport):
    await command(transport, '/trigger "word"', _source())
    update_chat_settings(transport.home, "group", GROUP, {"trigger_mode": "trigger"})
    guest = _source("300")
    event = _message("say word please", guest)
    assert await transport.adapter._group_triggered(event, guest) is False


@pytest.mark.asyncio
async def test_trigger_rejects_multiword_with_usage_note(transport):
    await command(transport, "/trigger a big trigger")
    text = transport.adapter._send_rich_ui.await_args.args[1]
    assert "Usage" in text and "one word only" in text
    assert load_chat_settings(transport.home, "group", GROUP)["trigger_word"] == ""


@pytest.mark.asyncio
async def test_trigger_off_disables_and_no_arg_shows_state(transport):
    await command(transport, '/trigger "word"')
    assert load_chat_settings(transport.home, "group", GROUP)["trigger_word"] == "word"

    transport.adapter._send_rich_ui.reset_mock()
    await command(transport, "/trigger")
    text = transport.adapter._send_rich_ui.await_args.args[1]
    assert "<h1>Triggers</h1>" in text
    assert "Current trigger is set to" in text
    assert 'type="copy_text" text="word"' in text
    assert "<hr>" in text
    assert "<details><summary>What triggers are</summary>" in text
    assert 'type="copy_text" text="/trigger &quot;off&quot;"' in text

    await command(transport, '/trigger "off"')
    assert load_chat_settings(transport.home, "group", GROUP)["trigger_word"] == ""

    transport.adapter._send_rich_ui.reset_mock()
    await command(transport, "/trigger")
    text = transport.adapter._send_rich_ui.await_args.args[1]
    assert "<h1>Triggers</h1>" in text
    assert "Current trigger is set to" in text
    assert 'type="copy_text" text="off"' in text
    assert 'type="copy_text" text="/trigger &quot;word&quot;"' in text


@pytest.mark.asyncio
async def test_trigger_is_group_only(transport):
    await command(transport, '/trigger "word"', _source(OWNER, chat_type="dm", chat_id=OWNER))
    assert "only in a group" in transport.adapter.send.await_args.args[1]
    assert load_chat_settings(transport.home, "group", GROUP)["trigger_word"] == ""


# (e) /allow and /disallow resolve reply, username, numeric ID --------------

@pytest.mark.asyncio
async def test_allow_resolves_replied_user(transport):
    event = _reply_message("/allow", _source(), "201")
    await transport.adapter._on_new_message(event)
    token = _confirmation_token(transport.adapter)
    await confirm(transport, token)
    assert transport.store.get_access("201")["allowed"] is True


@pytest.mark.asyncio
async def test_allow_resolves_username(transport):
    transport.adapter._client.get_entity = AsyncMock(return_value=SimpleNamespace(
        id=201, first_name="Started", last_name="User", bot=False,
    ))
    await command(transport, "/allow @starteduser")
    token = _confirmation_token(transport.adapter)
    await confirm(transport, token)
    assert transport.store.get_access("201")["allowed"] is True


@pytest.mark.asyncio
async def test_allow_resolves_numeric_id_and_unresolvable_shows_usage(transport):
    await command(transport, "/allow 201")
    token = _confirmation_token(transport.adapter)
    await confirm(transport, token)
    assert transport.store.get_access("201")["allowed"] is True

    transport.adapter._send_rich_ui.reset_mock()
    await command(transport, "/allow")
    assert "Usage" in transport.adapter._send_rich_ui.await_args.args[1]

    transport.adapter._client.get_entity = AsyncMock(side_effect=RuntimeError("no such user"))
    transport.adapter._send_rich_ui.reset_mock()
    await command(transport, "/allow @nobodyhere")
    assert "could not verify" in transport.adapter._send_rich_ui.await_args.args[1].lower()
    assert transport.store.get_access("201")["allowed"] is True


@pytest.mark.asyncio
async def test_disallow_resolves_replied_user(transport):
    transport.store.allow_user(Principal(OWNER), "201", True)
    event = _reply_message("/disallow", _source(), "201")
    await transport.adapter._on_new_message(event)
    assert transport.store.get_access("201")["allowed"] is False


@pytest.mark.asyncio
async def test_disallow_resolves_username(transport):
    transport.store.allow_user(Principal(OWNER), "202", True)
    transport.adapter._client.get_entity = AsyncMock(return_value=SimpleNamespace(
        id=202, first_name="Admin", last_name="User", bot=False,
    ))
    await command(transport, "/disallow @adminuser")
    assert transport.store.get_access("202")["allowed"] is False


@pytest.mark.asyncio
async def test_disallow_resolves_numeric_id_and_unresolvable_shows_usage(transport):
    transport.store.allow_user(Principal(OWNER), "203", True)
    await command(transport, "/disallow 203")
    assert transport.store.get_access("203")["allowed"] is False

    transport.adapter._send_rich_ui.reset_mock()
    await command(transport, "/disallow")
    assert "Usage" in transport.adapter._send_rich_ui.await_args.args[1]
    assert "numeric user id" in transport.adapter._send_rich_ui.await_args.args[1]

    transport.adapter._client.get_entity = AsyncMock(side_effect=RuntimeError("no such user"))
    transport.store.allow_user(Principal(OWNER), "202", True)
    transport.adapter._send_rich_ui.reset_mock()
    await command(transport, "/disallow @nobodyhere")
    assert "could not resolve" in transport.adapter._send_rich_ui.await_args.args[1].lower()
    assert transport.store.get_access("202")["allowed"] is True


# (f) Owner-only commands silently deny admitted non-owners -------------------

@pytest.mark.asyncio
async def test_settings_triggers_open_and_back_edits_same_message(transport):
    update_chat_settings(transport.home, "group", GROUP, {"trigger_word": "word"})
    source = _source()
    open_event = _callback("hm:settings:triggers:open", source)
    await transport.adapter._on_callback_query(open_event)
    open_event.answer.assert_awaited()
    transport.adapter._send_rich_ui.assert_not_awaited()
    edit = transport.adapter._edit_rich_ui_message.await_args
    assert edit is not None
    assert edit.args[0] is open_event
    assert "<h1>Triggers</h1>" in edit.args[2]
    assert _callback_payload(edit.args[3], "hm:settings:triggers:back") == "hm:settings:triggers:back"

    transport.adapter._edit_rich_ui_message.reset_mock()
    back_event = _callback("hm:settings:triggers:back", source)
    await transport.adapter._on_callback_query(back_event)
    back_event.answer.assert_awaited()
    transport.adapter._send_rich_ui.assert_not_awaited()
    edit = transport.adapter._edit_rich_ui_message.await_args
    assert edit is not None
    assert edit.args[0] is back_event
    assert edit.args[2].startswith("<h1>Group settings")
    assert "hm:settings:triggers:open" in edit.args[2]


@pytest.mark.asyncio
async def test_allowed_nonowner_allow_and_disallow_are_silently_denied(transport):
    start_grant(transport.store, "201", GROUP)
    transport.store.allow_user(Principal(OWNER), "203", True)
    for text in ("/allow 203", "/disallow 203"):
        await command(transport, text, _source("201"))
        transport.adapter.send.assert_not_awaited()
        transport.adapter._send_rich_ui.assert_not_awaited()
        transport.adapter.send_reaction.assert_not_awaited()
    assert transport.store.get_access("203")["allowed"] is True
    assert getattr(transport.adapter, "_pending_allow_requests", {}) == {}
