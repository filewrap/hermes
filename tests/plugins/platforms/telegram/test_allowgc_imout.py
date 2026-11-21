"""Behaviour contracts for /imout: ephemeral confirmation, opt-out,
re-admission on DM approval, and total (mirrored) bans.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from gateway.allowgc import (
    create_group_profile,
    load_state,
    resolve_home,
)
from gateway.config import PlatformConfig
from gateway.mongo_multiuser import store_for_home
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

OWNER = "100"
MEMBER = "201"
OTHER = "202"
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


def _message(text, source, *, message_id=55):
    return SimpleNamespace(
        source=source,
        message=SimpleNamespace(message=text, id=message_id, reply_to_msg_id=None),
        get_sender=AsyncMock(return_value=SimpleNamespace(
            id=int(source.user_id),
            username=f"user_{source.user_id}",
            first_name=f"User {source.user_id}",
            bot=source.is_bot,
        )),
    )


def _callback(data, source, *, message_id=901):
    return SimpleNamespace(
        source=source,
        data=data.encode(),
        message_id=message_id,
        answer=AsyncMock(),
    )


@pytest.fixture
def transport(tmp_path, monkeypatch):
    """A real local tenant store behind a mocked Telethon/UI transport.

    The resolver mirrors production routing: a group source with an active
    gc- profile resolves to that profile's home, anything else to the launch
    home.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / ".env").write_text(
        "OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=200\n", encoding="utf-8",
    )
    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": True}))
    adapter._bot_username = "NainaAssistantBot"
    adapter._bot_name = "Naina"
    adapter.send_reaction = AsyncMock(return_value=True)

    def _resolve(src):
        if getattr(src, "chat_type", None) in {"group", "forum"}:
            gc = resolve_home(getattr(src, "chat_id", None), reap=False)
            if gc is not None:
                return gc
        return home

    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=_resolve,
        _profile_name_for_source=lambda _source, adapter_profile=None: adapter_profile,
    )
    adapter._source = AsyncMock(side_effect=lambda event: event.source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter._send_rich_ui = AsyncMock(return_value=SimpleNamespace(id=55))
    adapter._edit_rich_ui_message = AsyncMock()
    adapter.send = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._input_peer_for = AsyncMock(
        side_effect=lambda uid: types.InputPeerUser(int(uid), 98765))
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=901))
    adapter._client.get_entity = AsyncMock()
    adapter._client.get_input_entity = AsyncMock(
        side_effect=lambda peer: types.InputUser(int(getattr(peer, "user_id", peer)), 98765))
    adapter._peer_for = lambda chat_id: types.InputPeerSelf()

    tenant_handler = AsyncMock(return_value={"final_response": "tenant reply"})
    monkeypatch.setattr("gateway.tenant_runtime.handle_tenant_message", tenant_handler)
    monkeypatch.setattr("plugins.platforms.telegram.incoming_reactions.react_to_incoming", AsyncMock())

    store = store_for_home(home)
    return SimpleNamespace(
        adapter=adapter, store=store, home=home, tenant_handler=tenant_handler,
    )


async def command(transport, text, source=None, *, message_id=55):
    await transport.adapter._on_new_message(
        _message(text, source or _source(), message_id=message_id))


def _ephemeral_sends(adapter):
    return [call.args[0] for call in adapter._client.await_args_list
            if isinstance(call.args[0], functions.ephemeral.SendMessageRequest)]


def _ephemeral_deletes(adapter):
    return [call.args[0] for call in adapter._client.await_args_list
            if isinstance(call.args[0], functions.ephemeral.DeleteMessageRequest)]


def _normal_sends(adapter):
    return [call.args[0] for call in adapter._client.await_args_list
            if isinstance(call.args[0], functions.messages.SendMessageRequest)]


def _admit_member(transport, user_id=MEMBER):
    """Grant a member in the group store through the real store API."""
    from agent.mongo_tenants import Principal

    gc_home = resolve_home(GROUP, reap=False)
    store_for_home(gc_home).allow_user(Principal(OWNER), user_id, True)


def _fail_ephemeral_dm(adapter):
    """Make the ephemeral DM send fail (the never-started-the-bot case).

    The real failure is ``BadRequestError: RPCError 400: BOT_NOT_ADMIN``
    from the ephemeral SendMessageRequest; any transport failure of that
    request takes the same path through ``send_imout_confirmation``.
    """

    async def _fail(request):
        if isinstance(request, functions.ephemeral.SendMessageRequest):
            raise RuntimeError("RPCError 400: BOT_NOT_ADMIN")
        return SimpleNamespace(id=901)

    adapter._client.side_effect = _fail


def _fallback_text():
    return (
        f"you've been kicked from this Chat {GROUP} profile. "
        "To regain access, start the bot."
    )


@pytest.mark.asyncio
async def test_imout_sends_ephemeral_confirm_not_normal_send(transport):
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await command(transport, "/imout", _source(MEMBER), message_id=77)

    sends = _ephemeral_sends(adapter)
    assert len(sends) == 1
    request = sends[0]
    assert request.peer.user_id == int(MEMBER)
    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    assert "once you leave, you leave" in request.rich_message.html
    assert GROUP in request.rich_message.html
    buttons = [(button.text, button.type.data.decode())
               for row in request.reply_markup.rows for button in row.buttons]
    assert ("Confirm leaving", f"hm:imout:confirm:{GROUP}") in buttons
    assert ("Cancel", f"hm:imout:cancel:{GROUP}") in buttons
    assert _normal_sends(adapter) == []
    adapter.send.assert_not_awaited()
    adapter._send_rich_ui.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()
    # Nothing is recorded yet: the DM Confirm still does the opt-out.
    assert load_state(resolve_home(GROUP, reap=False))["opted_out"] == []


@pytest.mark.asyncio
async def test_imout_confirm_opts_out_and_auto_admission_skips(transport):
    from agent.mongo_tenants import Principal

    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    await command(transport, "/imout", _source(MEMBER), message_id=77)

    member_dm = _source(MEMBER, chat_type="dm", chat_id=MEMBER)
    await adapter._on_callback_query(
        _callback(f"hm:imout:confirm:{GROUP}", member_dm))

    assert load_state(gc_home)["opted_out"] == [MEMBER]
    deletes = _ephemeral_deletes(adapter)
    assert len(deletes) == 1 and deletes[0].id == 901
    # The old grant is untouched (opt-out is a separate list), but the
    # member is never re-granted once it is gone, and gets no turn.
    group_store = store_for_home(gc_home)
    group_store.allow_user(Principal(OWNER), MEMBER, False)
    adapter.handle_message.reset_mock()
    transport.tenant_handler.reset_mock()
    await command(transport, "@NainaAssistantBot hello", _source(MEMBER))
    assert group_store.get_access(MEMBER)["allowed"] is False
    adapter.handle_message.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_imout_cancel_dismisses_without_opt_out(transport):
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    await command(transport, "/imout", _source(MEMBER), message_id=77)

    member_dm = _source(MEMBER, chat_type="dm", chat_id=MEMBER)
    callback = _callback(f"hm:imout:cancel:{GROUP}", member_dm)
    await adapter._on_callback_query(callback)

    assert load_state(gc_home)["opted_out"] == []
    assert len(_ephemeral_deletes(adapter)) == 1
    callback.answer.assert_awaited_once()
    assert "Cancelled" in callback.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_imout_undeliverable_dm_opts_out_and_replies_in_group(transport):
    """Undeliverable DM: the opt-out itself is completed in-group.

    The member never started the bot, so the ephemeral confirmation
    cannot be delivered (BOT_NOT_ADMIN). The /imout command is the
    intent; the DM card is only a confirmation mechanism. The removal
    must be recorded in the group profile state and the member's grant
    revoked, with the in-group reply only notifying of a removal that
    already happened.
    """
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    _fail_ephemeral_dm(adapter)

    await command(transport, "/imout", _source(MEMBER), message_id=77)

    assert len(_ephemeral_sends(adapter)) == 1
    assert load_state(gc_home)["opted_out"] == [MEMBER]
    assert store_for_home(gc_home).get_access(MEMBER)["allowed"] is False
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[0] == GROUP
    assert adapter.send.await_args.args[1] == _fallback_text()
    assert adapter.send.await_args.kwargs.get("reply_to") == 77
    adapter._send_rich_ui.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_imout_undeliverable_dm_and_failed_opt_out_sends_nothing(transport, monkeypatch):
    """If the opt-out write itself fails, nobody is told they are out.

    The fallback reply is only a notification of a recorded removal:
    when the write raises, the state still shows the member in, so no
    in-group message goes out (and no grant is revoked by the failed
    write either).
    """
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    _fail_ephemeral_dm(adapter)

    def _boom(*_args):
        raise OSError("state file unwritable")

    monkeypatch.setattr("gateway.allowgc.opt_out", _boom)

    await command(transport, "/imout", _source(MEMBER), message_id=77)

    assert load_state(gc_home)["opted_out"] == []
    adapter.send.assert_not_awaited()
    adapter._send_rich_ui.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_imout_fallback_removal_is_not_readmitted(transport):
    """After the fallback removal, the next group message is ignored.

    The recorded opt-out is consulted before auto-admission, so the
    member is not re-granted on their next message and gets no turn.
    """
    from gateway.allowgc import is_opted_out

    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    _fail_ephemeral_dm(adapter)
    await command(transport, "/imout", _source(MEMBER), message_id=77)
    assert is_opted_out(gc_home, MEMBER)

    adapter.send.reset_mock()
    adapter.handle_message.reset_mock()
    transport.tenant_handler.reset_mock()
    await command(transport, "@NainaAssistantBot hello", _source(MEMBER))

    assert store_for_home(gc_home).get_access(MEMBER)["allowed"] is False
    assert is_opted_out(gc_home, MEMBER)
    adapter.send.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_imout_fallback_removal_rejoin_needs_dm_start_and_owner_approval(transport):
    """The fallback opt-out is one-way: only DM /start + owner approval clears it."""
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    _fail_ephemeral_dm(adapter)
    await command(transport, "/imout", _source(MEMBER), message_id=77)
    assert load_state(gc_home)["opted_out"] == [MEMBER]

    adapter._send_start_welcome = AsyncMock()
    member_dm = _source(MEMBER, chat_type="dm", chat_id=MEMBER)
    await command(transport, "/start", member_dm)
    assert transport.store.resolve_started_user(MEMBER)
    # Merely starting does not clear the opt-out or re-admit.
    assert load_state(gc_home)["opted_out"] == [MEMBER]

    owner_dm = _source(OWNER, chat_type="dm", chat_id=OWNER)
    await command(transport, "/allow 201", owner_dm)
    adapter._send_rich_ui.assert_awaited()
    token = None
    for row in adapter._send_rich_ui.await_args.args[2].rows:
        for button in row.buttons:
            data = button.type.data.decode()
            if data.startswith("hm:allow:confirm:"):
                token = data.removeprefix("hm:allow:confirm:")
    assert token
    await adapter._on_callback_query(_callback(f"hm:allow:confirm:{token}", owner_dm, message_id=55))

    assert transport.store.get_access(MEMBER)["allowed"] is True
    assert load_state(gc_home)["opted_out"] == []


@pytest.mark.asyncio
async def test_imout_owner_in_non_allowgc_group_is_silent(transport):
    """Owner /imout where allowgc is not active never reaches a turn.

    Before the group profile exists the command fell through to the
    conversational turn (a Hindi "didn't understand" reply). It must be
    silently ignored like every other non-member /imout.
    """
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await command(transport, "/imout@NainaAssistantBot", _source(OWNER, chat_id=OTHER_GROUP), message_id=79)

    assert _ephemeral_sends(adapter) == []
    adapter.send.assert_not_awaited()
    adapter._send_rich_ui.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_imout_globally_banned_user_is_silent(transport):
    """A launch-banned user is never auto-admitted, so /imout stays silent."""
    from agent.mongo_tenants import Principal

    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    transport.store.ban_user(Principal(OWNER), OTHER, True)

    await command(transport, "/imout", _source(OTHER), message_id=78)

    assert _ephemeral_sends(adapter) == []
    adapter.send.assert_not_awaited()
    adapter._send_rich_ui.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_imout_in_non_allowgc_group_is_silent(transport):
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await command(transport, "/imout", _source(MEMBER, chat_id=OTHER_GROUP), message_id=79)

    assert _ephemeral_sends(adapter) == []
    adapter.send.assert_not_awaited()
    adapter._send_rich_ui.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_start_approval_clears_opt_out(transport):
    from gateway.allowgc import opt_out

    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    opt_out(gc_home, MEMBER)
    assert load_state(gc_home)["opted_out"] == [MEMBER]
    adapter._send_start_welcome = AsyncMock()
    member_dm = _source(MEMBER, chat_type="dm", chat_id=MEMBER)

    await command(transport, "/start", member_dm)
    assert transport.store.resolve_started_user(MEMBER)

    owner_dm = _source(OWNER, chat_type="dm", chat_id=OWNER)
    await command(transport, "/allow 201", owner_dm)
    adapter._send_rich_ui.assert_awaited()
    token = None
    for row in adapter._send_rich_ui.await_args.args[2].rows:
        for button in row.buttons:
            data = button.type.data.decode()
            if data.startswith("hm:allow:confirm:"):
                token = data.removeprefix("hm:allow:confirm:")
    assert token
    await adapter._on_callback_query(_callback(f"hm:allow:confirm:{token}", owner_dm, message_id=55))

    assert transport.store.get_access(MEMBER)["allowed"] is True
    assert load_state(gc_home)["opted_out"] == []


def test_handle_command_mirror_store_bans_both(transport):
    from plugins.platforms.telegram.multiuser import handle_command

    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    group_store = store_for_home(gc_home)
    launch_store = transport.store

    handled, response = handle_command(group_store, OWNER, "/ban 201", mirror_store=launch_store)
    assert handled and "banned" in response
    assert group_store.get_access(MEMBER)["banned"] is True
    assert launch_store.get_access(MEMBER)["banned"] is True

    handled, response = handle_command(group_store, OWNER, "/unban 201", mirror_store=launch_store)
    assert handled and "unbanned" in response
    assert group_store.get_access(MEMBER)["banned"] is False
    assert launch_store.get_access(MEMBER)["banned"] is False

    # Allow/revoke are never mirrored.
    handled, _response = handle_command(group_store, OWNER, "/allow 201", mirror_store=launch_store)
    assert handled
    assert group_store.get_access(MEMBER)["allowed"] is True
    assert launch_store.get_access(MEMBER)["allowed"] is False


@pytest.mark.asyncio
async def test_ban_in_allowgc_group_mirrors_to_launch_store(transport):
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await command(transport, "/ban 201", _source())

    group_store = store_for_home(gc_home)
    assert group_store.get_access(MEMBER)["banned"] is True
    assert transport.store.get_access(MEMBER)["banned"] is True
    adapter.send.assert_awaited_once()
    assert "banned" in adapter.send.await_args.args[1]

    await command(transport, "/unban 201", _source())
    assert group_store.get_access(MEMBER)["banned"] is False
    assert transport.store.get_access(MEMBER)["banned"] is False

    # A non-owner ban attempt changes nothing in either store.
    _admit_member(transport, OTHER)
    await command(transport, "/ban 201", _source(OTHER))
    assert group_store.get_access(MEMBER)["banned"] is False
    assert transport.store.get_access(MEMBER)["banned"] is False


@pytest.mark.asyncio
async def test_imout_silent_ignores_are_logged(transport, caplog):
    """The /imout path's silent ignores leave log lines with actor + chat
    id: the owner (never confirmed) in an active group, and any sender
    where the group has no allowgc profile."""
    import logging

    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    with caplog.at_level(logging.INFO):
        await command(transport, "/imout", _source(OWNER), message_id=80)
    assert "owner" in caplog.text
    assert OWNER in caplog.text and GROUP in caplog.text
    adapter.send.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()

    caplog.clear()
    with caplog.at_level(logging.INFO):
        await command(transport, "/imout", _source(MEMBER, chat_id=OTHER_GROUP), message_id=81)
    assert "no group home" in caplog.text
    assert MEMBER in caplog.text and OTHER_GROUP in caplog.text
    adapter.send.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


# --- Ephemeral /imout (in-group confirmation) -------------------------------


def _ephemeral_message(text, source, *, message_id=77):
    """A real layer-233 UpdateNewEphemeralMessage for a group command."""
    return types.UpdateNewEphemeralMessage(message=types.Message(
        id=message_id,
        peer_id=types.PeerChat(chat_id=abs(int(source.chat_id))),
        message=text,
        from_id=types.PeerUser(user_id=int(source.user_id)),
    ))


def _ephemeral_callback(data, source, *, message_id=901, query_id=5):
    """A real layer-233 UpdateEphemeralBotCallbackQuery for a group tap."""
    return types.UpdateEphemeralBotCallbackQuery(
        query_id=query_id,
        user_id=int(source.user_id),
        msg_id=message_id,
        data=data.encode(),
        message=types.Message(
            id=message_id,
            peer_id=types.PeerChat(chat_id=abs(int(source.chat_id))),
            message="Leave this group?",
            from_id=types.PeerUser(user_id=999),
        ),
        peer=types.PeerChat(chat_id=abs(int(source.chat_id))),
    )


def _ephemeral_edits(adapter):
    return [call.args[0] for call in adapter._client.await_args_list
            if isinstance(call.args[0], functions.ephemeral.EditMessageRequest)]


def _callback_answers(adapter):
    return [call.args[0] for call in adapter._client.await_args_list
            if isinstance(call.args[0], functions.messages.SetBotCallbackAnswerRequest)]


@pytest.mark.asyncio
async def test_ephemeral_imout_command_confirms_in_group_replying_to_command(transport):
    """An ephemeral /imout gets an ephemeral in-group confirmation that
    replies to the ephemeral command — never a DM, never a normal send."""
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await adapter._on_ephemeral_message(
        _ephemeral_message("/imout", _source(MEMBER), message_id=77))

    sends = _ephemeral_sends(adapter)
    assert len(sends) == 1
    request = sends[0]
    assert request.receiver_id.user_id == int(MEMBER)
    assert request.peer.user_id == int(GROUP)
    assert isinstance(request.reply_to, types.InputReplyToEphemeralMessage)
    assert request.reply_to.id == 77
    assert "once you leave, you leave" in request.rich_message.html
    buttons = [(button.text, button.type.data.decode())
               for row in request.reply_markup.rows for button in row.buttons]
    assert ("Confirm leaving", f"hm:imout:confirm:{GROUP}") in buttons
    assert ("Cancel", f"hm:imout:cancel:{GROUP}") in buttons
    assert _normal_sends(adapter) == []
    adapter.send.assert_not_awaited()
    assert load_state(resolve_home(GROUP, reap=False))["opted_out"] == []


@pytest.mark.asyncio
async def test_ephemeral_imout_confirm_edits_in_place_and_answers_silently(transport):
    """A Confirm tap edits the same ephemeral message in place and answers
    the callback silently: no toast text, no alert, no delete."""
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    await adapter._on_ephemeral_message(
        _ephemeral_message("/imout", _source(MEMBER), message_id=77))

    await adapter._on_ephemeral_callback(
        _ephemeral_callback(f"hm:imout:confirm:{GROUP}", _source(MEMBER)))

    assert load_state(gc_home)["opted_out"] == [MEMBER]
    edits = _ephemeral_edits(adapter)
    assert len(edits) == 1
    assert edits[0].id == 901
    assert edits[0].receiver_id.user_id == int(MEMBER)
    assert "You have left" in edits[0].message
    assert edits[0].reply_markup is None
    answers = _callback_answers(adapter)
    assert len(answers) == 1
    assert answers[0].query_id == 5
    assert answers[0].cache_time == 0
    assert not answers[0].message and not answers[0].alert
    assert _ephemeral_deletes(adapter) == []
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_ephemeral_imout_cancel_deletes_and_answers_silently(transport):
    """A Cancel tap deletes the ephemeral confirmation, answers silently,
    and records no opt-out."""
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    await adapter._on_ephemeral_message(
        _ephemeral_message("/imout", _source(MEMBER), message_id=77))

    await adapter._on_ephemeral_callback(
        _ephemeral_callback(f"hm:imout:cancel:{GROUP}", _source(MEMBER)))

    assert load_state(gc_home)["opted_out"] == []
    deletes = _ephemeral_deletes(adapter)
    assert len(deletes) == 1 and deletes[0].id == 901
    answers = _callback_answers(adapter)
    assert len(answers) == 1 and not answers[0].message and not answers[0].alert
    assert _ephemeral_edits(adapter) == []


@pytest.mark.asyncio
async def test_ephemeral_imout_undeliverable_confirmation_falls_back_direct(transport):
    """When the in-group ephemeral confirmation cannot be delivered at all,
    the opt-out is still recorded and the group is told — never a normal
    (non-ephemeral) confirmation instead."""
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    _fail_ephemeral_dm(adapter)

    await adapter._on_ephemeral_message(
        _ephemeral_message("/imout", _source(MEMBER), message_id=77))

    assert len(_ephemeral_sends(adapter)) == 1
    assert load_state(gc_home)["opted_out"] == [MEMBER]
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[0] == GROUP
    assert adapter.send.await_args.args[1] == _fallback_text()
    assert adapter.send.await_args.kwargs.get("reply_to") is None


@pytest.mark.asyncio
async def test_ephemeral_imout_ignores_owner_and_non_admitted(transport):
    """Owner and non-admitted ephemeral /imout are silently ignored."""
    adapter = transport.adapter
    create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)

    await adapter._on_ephemeral_message(
        _ephemeral_message("/imout", _source(OWNER), message_id=80))
    await adapter._on_ephemeral_message(
        _ephemeral_message("/imout", _source(OTHER), message_id=81))

    assert _ephemeral_sends(adapter) == []
    adapter.send.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_ephemeral_imout_callback_from_non_member_is_refused_silently(transport):
    """A tap from someone who is not an admitted member edits the card to a
    refusal, answers silently, and records nothing."""
    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    _admit_member(transport)
    await adapter._on_ephemeral_message(
        _ephemeral_message("/imout", _source(MEMBER), message_id=77))

    await adapter._on_ephemeral_callback(
        _ephemeral_callback(f"hm:imout:confirm:{GROUP}", _source(OTHER)))

    assert load_state(gc_home)["opted_out"] == []
    edits = _ephemeral_edits(adapter)
    assert len(edits) == 1 and "not available" in edits[0].message
    answers = _callback_answers(adapter)
    assert len(answers) == 1 and not answers[0].message and not answers[0].alert
