"""Focused regression coverage for Telegram's native access controls.

These tests deliberately use the profile-local SQLite tenant store and YAML
chat settings. Only Telegram transport/UI boundaries and the tenant message
handler are intercepted; the authorization and persistence logic stays real.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from agent.mongo_tenants import Principal
from gateway.config import PlatformConfig
from gateway.mongo_multiuser import store_for_home
from plugins.platforms.telegram.telegram_chat_settings import load_chat_settings
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


def _callback(data, source, *, message_id=55):
    return SimpleNamespace(
        source=source,
        data=data.encode(),
        message_id=message_id,
        answer=AsyncMock(),
    )


def _callback_payload(markup, prefix):
    """Return a native inline-button callback payload matching prefix."""
    for row in getattr(markup, "rows", ()) or ():
        for button in getattr(row, "buttons", ()) or ():
            data = getattr(getattr(button, "type", None), "data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if str(data).startswith(prefix):
                return str(data)
    raise AssertionError(f"No {prefix!r} callback button in native markup")


def _rich_callback_payload(message, prefix):
    """Return a callback payload embedded in an InputRichMessage button row."""
    for block in getattr(message, "blocks", ()) or ():
        for button in getattr(block, "buttons", ()) or ():
            data = getattr(getattr(button, "type", None), "data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if str(data).startswith(prefix):
                return str(data)
    raise AssertionError(f"No {prefix!r} callback button in native rich message")


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
    # Existing configured explicit users keep their global admin compatibility;
    # all other users receive no implicit grant from merely starting the bot.
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


@pytest.mark.asyncio
async def test_button_command_sends_native_gallery_only_after_multiuser_authorization(transport):
    source = _source(OWNER, chat_type="dm", chat_id=OWNER)
    await command(transport, "/button", source)

    requests = [call.args[0] for call in transport.adapter._client.await_args_list
                if isinstance(call.args[0], functions.messages.SendMessageRequest)]
    assert len(requests) == 2
    assert isinstance(requests[0].rich_message, types.InputRichMessage)
    assert isinstance(requests[0].reply_markup, types.ReplyInlineMarkup)
    assert isinstance(requests[1].rich_message, types.InputRichMessage)
    assert isinstance(requests[1].reply_markup, types.ReplyKeyboardMarkup)
    assert requests[1].reply_markup.single_use is True
    assert transport.adapter._button_demo_reply_keyboards[(OWNER, OWNER)]
    assert transport.adapter.send.await_count == 0
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_keyboard_tap_is_acknowledged_hidden_and_never_reaches_agent(transport):
    source = _source(OWNER, chat_type="dm", chat_id=OWNER)
    await command(transport, "/button", source)
    transport.adapter._client.reset_mock()

    await command(transport, "Reply blue", source)

    requests = [call.args[0] for call in transport.adapter._client.await_args_list
                if isinstance(call.args[0], functions.messages.SendMessageRequest)]
    assert len(requests) == 1
    assert isinstance(requests[0].rich_message, types.InputRichMessage)
    assert isinstance(requests[0].reply_markup, types.ReplyKeyboardHide)
    assert not transport.adapter._button_demo_reply_keyboards
    transport.tenant_handler.assert_not_awaited()
    transport.adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_another_command_hides_the_active_reply_keyboard_before_dispatch(transport):
    source = _source(OWNER, chat_type="dm", chat_id=OWNER)
    await command(transport, "/button", source)
    transport.adapter._client.reset_mock()

    await command(transport, "/help", source)

    requests = [call.args[0] for call in transport.adapter._client.await_args_list
                if isinstance(call.args[0], functions.messages.SendMessageRequest)]
    assert len(requests) >= 1
    assert isinstance(requests[0].reply_markup, types.ReplyKeyboardHide)
    assert "another command" in requests[0].rich_message.blocks[1].text.text
    assert not transport.adapter._button_demo_reply_keyboards


async def confirm(transport, token, source=None):
    source = source or _source()
    await transport.adapter._on_callback_query(
        _callback(f"hm:allow:confirm:{token}", source)
    )


def _recipient_notices(adapter, user_id="201"):
    return [call.args[0] for call in adapter._client.await_args_list
            if isinstance(call.args[0], functions.messages.SendMessageRequest)
            and getattr(call.args[0].peer, "user_id", None) == int(user_id)]


def _notice_peer(adapter):
    adapter._input_peer_for = AsyncMock(side_effect=lambda uid: types.InputPeerUser(int(uid), 0))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,chat", [("dm", OWNER), ("group", GROUP), ("forum", GROUP)])
async def test_allow_notifies_new_recipient_once_after_persisting_and_renders_safe_owner_card(transport, kind, chat):
    from html import escape

    adapter, store = transport.adapter, transport.store
    name = 'Mike <h1>Full & Name</h1> **not a heading**'
    store.record_started_identity("201", "starteduser", name)
    _notice_peer(adapter)
    source = _source(chat_type=kind, chat_id=chat)
    await command(transport, "/allow 201", source)
    assert not _recipient_notices(adapter)
    token = _confirmation_token(adapter)

    async def check_saved_before_delivery(request):
        assert store.get_access("201")["allowed"] is True
        return SimpleNamespace(id=901)

    adapter._client.side_effect = check_saved_before_delivery
    await confirm(transport, token, source)
    notice, = _recipient_notices(adapter)
    assert isinstance(notice.rich_message, types.InputRichMessageHTML)
    assert bytes(notice) and notice.message
    html = notice.rich_message.html
    assert escape(name) in html and '<h1>Full' not in html
    assert 'message me directly' in html and 'Naina' in html and '/help' in html
    assert html.count('<li>') == 3
    assert notice.reply_to is None and notice.no_webpage
    help_payload = _callback_payload(notice.reply_markup, 'hm:help:')
    assert help_payload == 'hm:help:menu:0'
    owner_html = adapter._edit_rich_ui_message.await_args.args[2]
    assert escape(name) in owner_html and '</b> can now message Naina directly.' in owner_html
    assert '<h2>Access granted</h2>' in owner_html
    assert "you're connected" not in owner_html.lower()
    assert 'show me around' not in owner_html.lower() and "you're in" not in owner_html.lower()
    assert 'stay separate from yours' in owner_html
    for forbidden in ('host OS', 'sudo', 'isolated runtime', '# Access', '\n#'):
        assert forbidden not in owner_html
    for authored in (html.replace(escape(name), ''), owner_html.replace(escape(name), '')):
        assert not re.search('[\U0001F000-\U0001FAFF\u2600-\u27BF]', authored)
    # The existing safe help callback works for the newly authorized recipient.
    adapter._edit_rich_ui = AsyncMock()
    await adapter._on_callback_query(_callback(help_payload, _source('201', chat_type='dm', chat_id='201')))
    adapter._edit_rich_ui.assert_awaited_once()
    # Token replay and repeated grants do not send duplicate or retroactive notices.
    await confirm(transport, token, source)
    await command(transport, '/allow 201', source)
    await confirm(transport, _confirmation_token(adapter), source)
    assert len(_recipient_notices(adapter)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('resolved_id', [201, 100, -100123])
async def test_notice_uses_real_private_peer_resolution_and_never_owner_or_group(transport, caplog, resolved_id):
    adapter, store = transport.adapter, transport.store
    del adapter._input_peer_for
    peer = (types.InputPeerChannel(abs(resolved_id), 55) if resolved_id < 0
            else types.InputPeerUser(resolved_id, 55))
    adapter._client.get_input_entity = AsyncMock(return_value=peer)
    await command(transport, '/allow 201', _source(chat_type='dm', chat_id=OWNER))
    await confirm(transport, _confirmation_token(adapter), _source(chat_type='dm', chat_id=OWNER))
    adapter._client.get_input_entity.assert_awaited_once_with(201)
    sends = [call.args[0] for call in adapter._client.await_args_list
             if isinstance(call.args[0], functions.messages.SendMessageRequest)]
    assert store.get_access('201')['allowed'] is True
    assert len(sends) == (1 if resolved_id == 201 else 0)
    if sends:
        assert sends[0].peer.user_id == 201 and sends[0].peer.user_id != int(OWNER)
        assert sends[0].reply_to is None
    else:
        assert 'grant remains active' in caplog.text
    assert '<h2>Access granted</h2>' in adapter._edit_rich_ui_message.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['blocked', 'timeout', 'private_reason', 'disconnected', 'peer'])
async def test_access_notice_failure_is_safe_actionable_and_never_revokes_saved_access(transport, caplog, failure):
    from telethon.errors import BadRequestError

    adapter, store = transport.adapter, transport.store
    _notice_peer(adapter)
    await command(transport, '/allow 201')
    token = _confirmation_token(adapter)

    async def reject_notice(request):
        if failure == 'timeout':
            raise TimeoutError('private body and credentials')
        if failure == 'private_reason':
            raise BadRequestError(request, 'PRIVATE body or credentials')
        raise BadRequestError(request, 'USER_IS_BLOCKED')

    adapter._client.side_effect = reject_notice
    if failure == 'disconnected':
        adapter._client = None
    elif failure == 'peer':
        adapter._input_peer_for.side_effect = ValueError('private body and credentials')
    await confirm(transport, token)
    assert store.get_access('201')['allowed'] is True
    assert store.access_for_scope('201', 'dm', '201')['sudo'] is True
    adapter._edit_rich_ui_message.assert_awaited_once()
    assert 'grant remains active' in caplog.text and '/help' in caplog.text
    assert 'private body' not in caplog.text and 'credentials' not in caplog.text
    assert 'SendMessageRequest(' not in caplog.text
    if failure == 'blocked':
        assert 'USER_IS_BLOCKED' in caplog.text
    if adapter._client is not None:
        assert len(_recipient_notices(adapter)) == (0 if failure == 'peer' else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['unknown', 'banned', 'ban_after_request', 'missing_start', 'expired', 'cancel', 'wrong_actor', 'wrong_chat', 'persistence'])
async def test_no_recipient_notice_when_access_is_not_granted(transport, monkeypatch, failure):
    adapter, store = transport.adapter, transport.store
    _notice_peer(adapter)
    if failure == 'unknown':
        await command(transport, '/allow 987654321')
    elif failure == 'banned':
        store.ban_user(Principal(OWNER), '201', True)
        await command(transport, '/allow 201')
    else:
        await command(transport, '/allow 201')
        token = _confirmation_token(adapter)
        if failure == 'ban_after_request':
            store.ban_user(Principal(OWNER), '201', True)
        elif failure == 'missing_start':
            store._profiles.update_one({'user_id': '201'}, {'$set': {'start_count': 0}})
        elif failure == 'expired':
            adapter._pending_allow_requests[token]['created'] -= 601
        elif failure == 'persistence':
            monkeypatch.setattr(type(store), 'allow_user', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('save failed')))
        if failure == 'cancel':
            await adapter._on_callback_query(_callback(f'hm:allow:cancel:{token}', _source()))
        else:
            actor = _source('202') if failure == 'wrong_actor' else _source(chat_id=OTHER_GROUP) if failure == 'wrong_chat' else _source()
            if failure == 'wrong_actor':
                start_grant(store, '202', GROUP)
            await confirm(transport, token, actor)
    assert not store.get_access('201')['allowed']
    assert not _recipient_notices(adapter)
    adapter._input_peer_for.assert_not_awaited()


@pytest.mark.parametrize('isolated', [True, False])
def test_allow_success_serializes_native_html_with_literal_names(isolated):
    from html import escape
    from plugins.platforms.telegram.access_ui import allow_success_view
    from plugins.platforms.telegram.mtproto_rich import edit_request

    name = 'A <b>Full & Name</b>\n# not a heading'
    html = allow_success_view({'id': '201', 'name': name}, isolated=isolated, chat_label='Group <name>')
    request = edit_request(types.InputPeerUser(100, 0), 55, html)
    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    assert bytes(request)
    assert escape(name) in request.rich_message.html
    assert '<b>Full & Name</b>' not in request.rich_message.html
    assert '<p><b>' in html and '</b> can now' in html and html.endswith('</p>')
    if isolated:
        assert 'stay separate from yours' in html
    else:
        assert 'Group &lt;name&gt;' in html and 'Access elsewhere is unchanged' in html


@pytest.mark.asyncio
async def test_access_notice_regrant_after_revocation_is_a_new_grant(transport):
    adapter, store = transport.adapter, transport.store
    _notice_peer(adapter)
    store.allow_user(Principal(OWNER), '201', True)
    store.allow_user(Principal(OWNER), '201', False)
    await command(transport, '/allow 201')
    await confirm(transport, _confirmation_token(adapter))
    assert store.get_access('201')['allowed']
    assert len(_recipient_notices(adapter)) == 1


@pytest.mark.asyncio
async def test_access_notice_and_grant_stay_in_owning_profile_a_b_a(transport, monkeypatch):
    import copy

    adapter_a, store_a = transport.adapter, transport.store
    home_b = transport.home / 'separate-profile'
    home_b.mkdir()
    (home_b / '.env').write_text('OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100\n')
    monkeypatch.setenv('HERMES_HOME', str(home_b))
    store_b = store_for_home(home_b)
    store_b.start_user('201')
    store_b.record_started_identity('201', 'starteduser', 'Same account, different profile')
    adapter_b = TelethonAdapter(PlatformConfig(extra={}))
    adapter_b.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda source: home_b)
    adapter_b._source = AsyncMock(side_effect=lambda event: event.source)
    adapter_b._cache_inbound_peer = AsyncMock()
    adapter_b._client = AsyncMock(return_value=SimpleNamespace(id=902))
    adapter_b._send_rich_ui = AsyncMock(return_value=SimpleNamespace(id=55))
    adapter_b._edit_rich_ui_message = AsyncMock()
    profile_b = copy.copy(transport)
    profile_b.adapter, profile_b.store, profile_b.home = adapter_b, store_b, home_b
    _notice_peer(adapter_a)
    _notice_peer(adapter_b)

    monkeypatch.setenv('HERMES_HOME', str(transport.home))
    await command(transport, '/allow 201')
    await confirm(transport, _confirmation_token(adapter_a))
    assert store_a.get_access('201')['allowed']
    assert not store_b.get_access('201')['allowed']
    assert len(_recipient_notices(adapter_a)) == 1 and not _recipient_notices(adapter_b)

    monkeypatch.setenv('HERMES_HOME', str(home_b))
    await command(profile_b, '/allow 201')
    assert not store_b.get_access('201')['allowed']
    token_b = _confirmation_token(adapter_b)

    monkeypatch.setenv('HERMES_HOME', str(transport.home))
    await command(transport, '/allow 201')
    await confirm(transport, _confirmation_token(adapter_a))
    assert len(_recipient_notices(adapter_a)) == 1
    assert not store_b.get_access('201')['allowed']

    monkeypatch.setenv('HERMES_HOME', str(home_b))
    await confirm(profile_b, token_b)
    assert store_b.get_access('201')['allowed']
    assert len(_recipient_notices(adapter_b)) == 1
    assert len(_recipient_notices(adapter_a)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [OWNER, "201"])
async def test_settings_dm_opens_native_personal_menu_for_owner_and_scoped_user(transport, user_id):
    if user_id != OWNER:
        start_grant(transport.store, user_id, "dm")
    await command(transport, "/settings", _source(user_id, chat_type="dm", chat_id=user_id))
    transport.tenant_handler.assert_not_awaited()
    transport.adapter._client.assert_awaited_once()
    assert transport.adapter._settings_ui()._states


@pytest.mark.asyncio
async def test_gsettings_in_group_opens_native_rich_controls_for_owner_and_authorized_user(transport):
    start_grant(transport.store, "202", GROUP)
    for actor in (OWNER, "202"):
        transport.adapter._send_rich_ui.reset_mock()
        await command(transport, "/gsettings", _source(actor))
        transport.adapter._send_rich_ui.assert_awaited()
        assert "group" in transport.adapter._send_rich_ui.await_args.args[1].casefold()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_gsettings_rejects_dm_unauthorized_and_banned_users(transport):
    await command(transport, "/gsettings", _source(OWNER, chat_type="dm", chat_id=OWNER))
    assert "only in a group" in transport.adapter.send.await_args.args[1].casefold()

    transport.adapter.send.reset_mock()
    await command(transport, "/gsettings", _source("201"))
    transport.adapter.send.assert_not_awaited()
    transport.adapter.send_reaction.assert_not_awaited()

    transport.store.ban_user(Principal(OWNER), "202", True)
    transport.adapter.send.reset_mock()
    await command(transport, "/gsettings", _source("202"))
    transport.adapter.send.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_gsetting_enabled_false_saves_only_current_group_and_rejects_other_group_id(transport):
    start_grant(transport.store, "202", GROUP)
    await command(transport, "/gsettings enabled=false", _source("202"))
    assert transport.store.get_group_settings(Principal(OWNER), GROUP)["enabled"] is False

    await command(transport, f"/gsettings {OTHER_GROUP} enabled=true", _source("202"))
    assert transport.store.get_group_settings(Principal(OWNER), OTHER_GROUP) is None
    assert transport.store.get_group_settings(Principal(OWNER), GROUP)["enabled"] is False
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_allow_numeric_started_user_waits_for_native_confirmation(transport):
    await command(transport, "/allow 201")
    transport.adapter._send_rich_ui.assert_awaited_once()
    token = _confirmation_token(transport.adapter)
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is False
    assert transport.store.access_for_scope("201", "dm", "201")["sudo"] is False

    await confirm(transport, token)
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is True
    assert transport.store.access_for_scope("201", "group", OTHER_GROUP)["sudo"] is True
    assert transport.store.access_for_scope("201", "dm", "201")["sudo"] is True
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_allow_unknown_numeric_user_never_provisions_or_grants(transport):
    await command(transport, "/allow 987654321")
    assert "must first send" in transport.adapter._send_rich_ui.await_args.args[1].casefold()
    assert not any(str(row.get("user_id")) == "987654321" for row in transport.store._profiles.find({}))
    assert transport.store.access_for_scope("987654321", "group", GROUP)["sudo"] is False
    assert getattr(transport.adapter, "_pending_allow_requests", {}) == {}
    transport.adapter._client.get_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_allow_username_resolves_current_entity_id_not_stale_stored_username(transport):
    # The old username remains in the started profile, but Telegram now assigns
    # it to an unstarted account. Only the live entity's numeric identity counts.
    transport.adapter._client.get_entity.return_value = SimpleNamespace(
        id=987654321, bot=False, first_name="Reassigned",
    )
    await command(transport, "/allow @starteduser")
    transport.adapter._client.get_entity.assert_awaited_once_with("@starteduser")
    assert "must first send" in transport.adapter._send_rich_ui.await_args.args[1].casefold()
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is False
    assert not any(str(row.get("user_id")) == "987654321" for row in transport.store._profiles.find({}))


@pytest.mark.asyncio
async def test_allow_changed_username_uses_live_id_then_canonical_started_record(transport):
    # A started user may change their username. The current Telegram resolution
    # produces the stable id; the canonical /start record is looked up by that id.
    transport.adapter._client.get_entity.return_value = SimpleNamespace(
        id=201, bot=False, first_name="User 201",
    )
    await command(transport, "/allow @newstartedname")
    transport.adapter._client.get_entity.assert_awaited_once_with("@newstartedname")
    token = _confirmation_token(transport.adapter)
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is False
    await confirm(transport, token)
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is True


@pytest.mark.asyncio
async def test_allow_username_lookup_failure_does_not_fall_back_to_stored_name(transport):
    transport.adapter._client.get_entity.side_effect = RuntimeError("not found")
    await command(transport, "/allow @starteduser")
    assert "could not verify" in transport.adapter._send_rich_ui.await_args.args[1].casefold()
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is False
    assert getattr(transport.adapter, "_pending_allow_requests", {}) == {}


@pytest.mark.asyncio
async def test_allow_ignores_bot_actor(transport):
    await command(transport, "/allow 201", _source(OWNER, is_bot=True))
    transport.adapter._send_rich_ui.assert_not_awaited()
    transport.adapter._client.get_entity.assert_not_awaited()
    assert getattr(transport.adapter, "_pending_allow_requests", {}) == {}
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is False


@pytest.mark.asyncio
async def test_allowed_user_cannot_grant_universal_access_to_someone_else(transport):
    start_grant(transport.store, "202", GROUP)
    await command(transport, "/allow 201", _source("202"))
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is False
    assert getattr(transport.adapter, "_pending_allow_requests", {}) == {}
    assert "owner" in str(transport.adapter._send_rich_ui.await_args).lower()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_source", [
    _source("202"),
    _source(OWNER, chat_id=OTHER_GROUP),
])
async def test_allow_confirmation_is_bound_to_requesting_user_and_chat(transport, wrong_source):
    start_grant(transport.store, "202", GROUP)
    await command(transport, "/allow 201")
    token = _confirmation_token(transport.adapter)
    # The other actor is independently authorized, so rejection demonstrates
    # binding to the original requester rather than only general authorization.
    if wrong_source.user_id == "202":
        start_grant(transport.store, "202", GROUP)
    await confirm(transport, token, wrong_source)
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is False
    assert transport.adapter._pending_allow_requests[token]["owner_id"] == OWNER


@pytest.mark.asyncio
async def test_stale_allow_confirmation_does_not_grant(transport):
    await command(transport, "/allow 201")
    token = _confirmation_token(transport.adapter)
    transport.adapter._pending_allow_requests[token]["created"] -= 601
    await confirm(transport, token)
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is False


@pytest.mark.asyncio
async def test_banned_target_is_rejected_at_confirmation_time(transport):
    await command(transport, "/allow 201")
    token = _confirmation_token(transport.adapter)
    transport.store.ban_user(Principal(OWNER), "201", True)
    await confirm(transport, token)
    access = transport.store.access_for_scope("201", "group", GROUP)
    assert access["banned"] is True
    assert access["sudo"] is False


@pytest.mark.asyncio
async def test_person_grants_apply_across_dm_and_groups_in_this_profile(transport):
    await command(transport, "/allow 201")
    group_token = _confirmation_token(transport.adapter)
    await confirm(transport, group_token)

    await command(transport, "/allow 201", _source(OWNER, chat_type="dm", chat_id=OWNER))
    dm_token = _confirmation_token(transport.adapter)
    await confirm(transport, dm_token, _source(OWNER, chat_type="dm", chat_id=OWNER))

    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is True
    assert transport.store.access_for_scope("201", "group", OTHER_GROUP)["sudo"] is True
    assert transport.store.access_for_scope("201", "dm", OWNER)["sudo"] is True
    assert transport.store.access_for_scope("201", "dm", "201")["sudo"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("action,initial,expected", [
    ("on", False, True),
    ("off", True, False),
    ("toggle", False, True),
    ("toggle", True, False),
])
async def test_allowall_persists_on_off_and_toggle_to_profile_yaml(transport, action, initial, expected):
    if initial:
        from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings
        update_chat_settings(transport.home, "group", GROUP, {"allow_all": True})
    await command(transport, f"/allowall {action}")
    settings = load_chat_settings(transport.home, "group", GROUP)
    assert settings["allow_all"] is expected
    assert load_chat_settings(transport.home, "group", OTHER_GROUP)["allow_all"] is False
    assert transport.adapter._send_rich_ui.await_count == 1
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowall_without_arguments_shows_current_status_without_mutation(transport):
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings
    update_chat_settings(transport.home, "group", GROUP, {"allow_all": True})
    await command(transport, "/allowall")
    assert "allow all · on" in transport.adapter._send_rich_ui.await_args.args[1].casefold()
    assert load_chat_settings(transport.home, "group", GROUP)["allow_all"] is True


@pytest.mark.asyncio
async def test_allowall_rejects_dm_and_unauthorized_group_actor(transport):
    await command(transport, "/allowall on", _source(OWNER, chat_type="dm", chat_id=OWNER))
    assert "only in a group" in transport.adapter.send.await_args.args[1].casefold()
    assert load_chat_settings(transport.home, "group", GROUP)["allow_all"] is False

    transport.adapter.send.reset_mock()
    await command(transport, "/allowall on", _source("201"))
    transport.adapter.send.assert_not_awaited()
    transport.adapter.send_reaction.assert_not_awaited()
    assert load_chat_settings(transport.home, "group", GROUP)["allow_all"] is False


@pytest.mark.asyncio
async def test_public_allowall_access_does_not_enable_admin_commands_or_callbacks(transport):
    await command(transport, "/allowall on")
    access = transport.store.access_for_scope("201", "group", GROUP)
    assert access["allowed"] is True
    assert access["sudo"] is False

    transport.adapter.send.reset_mock()
    await command(transport, "/allow 203", _source("201"))
    assert "explicitly authorized" in transport.adapter.send.await_args.args[1].casefold()
    assert getattr(transport.adapter, "_pending_allow_requests", {}) == {}

    callback = _callback("hm:settings:allowall:off", _source("201"))
    await transport.adapter._on_callback_query(callback)
    callback.answer.assert_awaited()
    assert load_chat_settings(transport.home, "group", GROUP)["allow_all"] is True
    assert transport.store.access_for_scope("201", "group", GROUP)["sudo"] is False


@pytest.mark.asyncio
async def test_unrelated_bot_group_chatter_never_creates_access_prompt_or_pending_grant(transport):
    await command(transport, "just a bot mentioning another service", _source(OWNER, is_bot=True))
    transport.adapter.send.assert_not_awaited()
    transport.adapter._send_rich_ui.assert_not_awaited()
    assert getattr(transport.adapter, "_pending_allow_requests", {}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['group', 'forum'])
@pytest.mark.parametrize('actor', [OWNER, '200'])
async def test_gsettings_accepts_owner_and_existing_configured_users_in_group_and_forum(transport, kind, actor):
    await command(transport, '/gsettings@NainaAssistantBot', _source(actor, chat_type=kind))
    transport.adapter._send_rich_ui.assert_awaited_once()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['group', 'forum'])
async def test_settings_is_never_a_group_menu(transport, kind):
    await command(transport, '/settings@NainaAssistantBot', _source(chat_type=kind))
    assert 'private' in transport.adapter.send.await_args.args[1].lower()
    transport.adapter._client.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "forum"])
@pytest.mark.parametrize('mode,text,expected', [
    ('direct', 'ordinary group message', True),
    ('tag', 'ordinary group message', False),
    ('tag', '@NainaAssistantBot hello', True),
    ('trigger', 'Naina hello', True),
    ('trigger', 'unaddressed hello', False),
])
async def test_group_mode_callback_changes_real_invocation_gate(transport, kind, mode, text, expected):
    adapter = transport.adapter
    adapter._bot_name = 'Naina'
    source = _source(chat_type=kind)
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings
    update_chat_settings(transport.home, 'group', GROUP, {'allow_all': True})
    await adapter._on_callback_query(_callback('hm:settings:mode:' + mode, source))
    if mode == "direct":
        assert load_chat_settings(transport.home, 'group', GROUP)['trigger_mode'] != "direct"
        token = next(iter(adapter._pending_direct_mode))
        await adapter._on_callback_query(_callback(f"hm:settings:direct:{token}:confirm", source))
    assert load_chat_settings(transport.home, 'group', GROUP)['trigger_mode'] == mode
    assert await adapter._group_triggered(_message(text, source), source) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_all", [False, True])
async def test_out_of_trigger_group_messages_are_silently_ignored(transport, allow_all):
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings

    adapter = transport.adapter
    source = _source("201", chat_type="group", chat_id=GROUP)
    adapter._bot_username = "NainaAssistantBot"
    update_chat_settings(transport.home, "group", GROUP, {
        "allow_all": allow_all, "trigger_mode": "tag",
    })
    await adapter._on_new_message(_message("ordinary untagged question", source))

    adapter.send.assert_not_awaited()
    adapter.send_reaction.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()
    adapter._send_rich_ui.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_sender_id, expected", [(42, True), (99, False)])
async def test_name_trigger_group_only_admits_replies_to_own_message_when_allow_all_off(
    transport, reply_sender_id, expected,
):
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings

    adapter = transport.adapter
    adapter._bot_id = 42
    adapter._authorized = lambda _source: True
    adapter._is_explicitly_allowed = lambda _source: True
    source = _source(OWNER, chat_type="group", chat_id=GROUP)
    update_chat_settings(transport.home, "group", GROUP, {
        "allow_all": False, "trigger_mode": "trigger",
    })
    message = _message("follow-up without a name or command", source)
    message.message.reply_to_msg_id = 54
    message.message.get_reply_message = AsyncMock(return_value=SimpleNamespace(
        sender_id=reply_sender_id, message="previous message",
    ))

    await adapter._on_new_message(message)

    if expected:
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.reply_to_is_own_message is True
        assert "You are the Telegram assistant @NainaAssistantBot" in event.channel_prompt
    else:
        adapter.handle_message.assert_not_awaited()
        adapter.send.assert_not_awaited()
        adapter.send_reaction.assert_not_awaited()
        adapter._send_rich_ui.assert_not_awaited()


@pytest.mark.asyncio
async def test_started_dm_grantee_is_welcomed_as_authorized_and_identity_is_refreshed(transport):
    transport.adapter._send_start_welcome = AsyncMock()
    start_grant(transport.store, '201', 'dm')
    await command(transport, '/start', _source('201', chat_type='dm', chat_id='201'))
    # The welcome is now Rich HTML with an in-body profile button.
    assert transport.adapter._send_start_welcome.await_count == 1
    card = transport.adapter._send_start_welcome.await_args.args[1]
    assert isinstance(card, str)
    assert "I'm Naina, an AI assistant." in card
    assert "tg://user?id=201" in card
    assert "<code>/help</code>" in card
    assert "<details>" not in card
    assert transport.store.resolve_started_user('201')['username'] == 'user_201'


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "forum"])
async def test_successful_allow_confirmation_cannot_regrant_after_revoke(transport, kind):
    source = _source(chat_type=kind)
    source.thread_id = "101" if kind == "forum" else None
    await command(transport, "/allow 201", source)
    token = _confirmation_token(transport.adapter)
    await confirm(transport, token, source)
    assert transport.store.access_for_scope("201", "dm", "201")["sudo"] is True
    transport.store.allow_user(Principal(OWNER), "201", False)
    await confirm(transport, token, source)
    for chat_type, chat in (("dm", "201"), (kind, GROUP), ("group", OTHER_GROUP)):
        access = transport.store.access_for_scope("201", chat_type, chat)
        assert access["allowed"] is False and access["sudo"] is False
    transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "forum"])
@pytest.mark.parametrize("action", ["revoke", "ban"])
@pytest.mark.parametrize("allow_all", [False, True])
async def test_removed_identity_cannot_reuse_group_controls_or_gain_dm_access(transport, kind, action, allow_all):
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings
    owner = Principal(OWNER)
    transport.store.allow_user(owner, "201", True)
    source = _source("201", chat_type=kind)
    source.thread_id = "101" if kind == "forum" else None
    await command(transport, "/gsettings", source)  # Authorized controls work with allowall OFF.
    transport.adapter._send_rich_ui.assert_awaited()
    if action == "revoke":
        transport.store.allow_user(owner, "201", False)
    else:
        transport.store.ban_user(owner, "201", True)
    update_chat_settings(transport.home, "group", GROUP, {"allow_all": allow_all, "trigger_mode": "tag"})
    source.thread_id = "102" if kind == "forum" else None
    transport.adapter._send_rich_ui.reset_mock()
    await command(transport, "/gsettings", source)
    transport.adapter._send_rich_ui.assert_not_awaited()
    for payload in ("hm:settings:allowall:toggle", "hm:settings:mode:direct"):
        callback = _callback(payload, source)
        await transport.adapter._on_callback_query(callback)
        callback.answer.assert_awaited()
    settings = load_chat_settings(transport.home, "group", GROUP)
    assert settings["allow_all"] is allow_all and settings["trigger_mode"] == "tag"
    await command(transport, "@NainaAssistantBot hello", _source("201", chat_type="dm", chat_id="201"))
    transport.tenant_handler.assert_not_awaited()  # Group openness never grants DM access.
    await command(transport, "@NainaAssistantBot hello", source)
    if action == "revoke" and allow_all:
        # Revoked global grants become unlisted group guests, never administrators.
        transport.tenant_handler.assert_awaited_once()
        assert transport.store.access_for_scope("201", kind, GROUP)["sudo"] is False
    else:
        transport.tenant_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,bot_name,expected_name,expected_bot",
    [
        ("A <b>Full & Name</b>", "Naina & Co", "A <b>Full & Name</b>", "Naina & Co"),
        ("", "", "Telegram user 201", "Naina"),
    ],
)
async def test_unallowed_private_start_sends_native_owner_pending_notice(name, bot_name, expected_name, expected_bot, transport):
    from telethon import types

    adapter = transport.adapter
    adapter._send_start_welcome = AsyncMock()
    adapter._bot_name = bot_name
    source = _source("201", chat_type="dm", chat_id="201")
    source.user_name = name

    await command(transport, "/start", source)

    adapter._send_start_welcome.assert_awaited_once()
    adapter._send_rich_ui.assert_awaited_once()
    owner_source, rich, reply_markup = adapter._send_rich_ui.await_args.args[:3]
    assert owner_source.user_id == OWNER and owner_source.chat_id == OWNER
    assert owner_source.chat_type == "dm"
    assert isinstance(rich, types.InputRichMessage)
    assert reply_markup is None
    first_paragraph = rich.blocks[0].text
    assert isinstance(first_paragraph, types.TextConcat)
    assert first_paragraph.texts[0].text == expected_name
    assert first_paragraph.texts[1].text == " ("
    assert isinstance(first_paragraph.texts[2], types.TextFixed)
    assert first_paragraph.texts[2].text.text == "201"
    assert first_paragraph.texts[3].text == f") started {expected_bot}."
    assert rich.blocks[1].text.text == "Actions you can take:"
    notice_text = f"{expected_name} (201) started {expected_bot}."
    assert not any(token in notice_text for token in ("<h1", "<h2", "🥴", "✅", "⚠"))
    assert not re.search('[\U0001F000-\U0001FAFF\u2600-\u27BF]', notice_text)
    assert transport.store.resolve_started_user("201")
    assert not transport.store.get_access("201")["allowed"]


@pytest.mark.asyncio
async def test_pending_start_notice_serializes_as_native_rich_message_with_styled_button_row(transport):
    adapter = transport.adapter
    adapter._send_start_welcome = AsyncMock()
    source = _source("201", chat_type="dm", chat_id="201")
    source.user_name = "Literal <name> & text"
    await command(transport, "/start", source)
    _owner_source, rich, reply_markup = adapter._send_rich_ui.await_args.args[:3]

    assert isinstance(rich, types.InputRichMessage)
    assert reply_markup is None
    assert isinstance(rich.blocks[2], types.PageBlockButtonRow)
    buttons = rich.blocks[2].buttons
    assert [button.text.text for button in buttons] == ["Ban", "Allow"]
    assert [button.type.data.decode() for button in buttons] == [
        "hm:admission:ban:201", "hm:admission:allow:201",
    ]
    assert buttons[0].style.bg_danger is True
    assert buttons[1].style.bg_success is True
    assert bytes(rich)
    first_paragraph = rich.blocks[0].text
    assert first_paragraph.texts[0].text == "Literal <name> & text"
    assert isinstance(first_paragraph.texts[2], types.TextFixed)
    assert first_paragraph.texts[2].text.text == "201"
    assert first_paragraph.texts[3].text == ") started Naina."


@pytest.mark.asyncio
async def test_repeated_private_start_keeps_user_pending_and_resends_owner_controls(transport):
    adapter = transport.adapter
    adapter._send_start_welcome = AsyncMock()
    source = _source("201", chat_type="dm", chat_id="201")

    await command(transport, "/start", source)
    first_rich = adapter._send_rich_ui.await_args.args[1]
    await command(transport, "/start", source)

    assert adapter._send_rich_ui.await_count == 2
    assert _rich_callback_payload(first_rich, "hm:admission:allow:") == "hm:admission:allow:201"
    assert _rich_callback_payload(adapter._send_rich_ui.await_args.args[1], "hm:admission:ban:") == "hm:admission:ban:201"
    assert transport.store.resolve_started_user("201")
    assert not transport.store.get_access("201")["allowed"]
    assert not transport.store.get_access("201")["banned"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,text",
    [
        (_source("200", chat_type="dm", chat_id="200"), "/start"),
        (_source(OWNER, chat_type="dm", chat_id=OWNER), "/start"),
        (_source("201", chat_type="group", chat_id=GROUP), "/start"),
    ],
)
async def test_allowed_owner_and_group_start_do_not_send_admission_controls(source, text, transport):
    adapter = transport.adapter
    adapter._send_start_welcome = AsyncMock()

    await command(transport, text, source)

    adapter._send_rich_ui.assert_not_awaited()
    if source.chat_type == "group":
        adapter.send.assert_awaited_once()
        assert "private chat" in adapter.send.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["201", "200"])
@pytest.mark.parametrize("action", ["allow", "ban"])
async def test_admission_callbacks_reject_ordinary_and_allowed_nonowner_actors(actor, action, transport):
    from plugins.platforms.telegram.telegram_controls import handle_owner_callback

    data = f"hm:admission:{action}:201"
    event = _callback(data, _source(actor, chat_type="dm", chat_id=actor))
    handled = await handle_owner_callback(
        transport.adapter, event, event.source, data,
    )

    assert handled is True
    event.answer.assert_awaited()
    assert not transport.store.get_access("201")["banned"]
    assert not transport.store.get_access("201")["allowed"]
    transport.adapter._send_rich_ui.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        _source(OWNER, chat_type="group", chat_id=GROUP),
        _source(OWNER, chat_type="dm", chat_id="201"),
    ],
)
@pytest.mark.parametrize("action", ["allow", "ban"])
async def test_admission_callbacks_reject_owner_group_and_nonself_private_chat(source, action, transport):
    from plugins.platforms.telegram.telegram_controls import handle_owner_callback

    data = f"hm:admission:{action}:201"
    event = _callback(data, source)
    handled = await handle_owner_callback(
        transport.adapter, event, source, data,
    )

    assert handled is True
    event.answer.assert_awaited()
    assert not transport.store.get_access("201")["banned"]
    assert not transport.store.get_access("201")["allowed"]
    transport.adapter._send_rich_ui.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_ban_callback_uses_real_store_and_banned_user_is_rechecked_at_start(transport):
    adapter, store = transport.adapter, transport.store
    adapter._send_start_welcome = AsyncMock()
    owner_dm = _source(OWNER, chat_type="dm", chat_id=OWNER)
    callback = _callback("hm:admission:ban:201", owner_dm)

    await adapter._on_callback_query(callback)

    assert store.get_access("201")["banned"] is True
    assert callback.answer.await_count == 1
    assert "Applying ban" in callback.answer.await_args.args[0]
    edits = [call.args[0] for call in adapter._client.await_args_list
             if getattr(call.args[0], "rich_message", None) is not None]
    assert len(edits) == 2
    progress = getattr(edits[0].rich_message, "html", None) or getattr(edits[0].rich_message, "markdown", "")
    outcome = getattr(edits[1].rich_message, "html", None) or getattr(edits[1].rich_message, "markdown", "")
    assert "Working" in progress and "Applying the access ban" in progress
    assert "Ban complete" in outcome and "User 201 banned." in outcome
    adapter._send_rich_ui.reset_mock()
    await command(transport, "/start", _source("201", chat_type="dm", chat_id="201"))
    adapter._send_rich_ui.assert_not_awaited()
    adapter._send_start_welcome.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["admission", "dm", "group"])
@pytest.mark.parametrize("action", ["confirm", "cancel"])
@pytest.mark.parametrize("target", ["201", "203"])
async def test_allow_uses_one_native_message_from_confirmation_through_result(transport, origin, action, target):
    adapter, store = transport.adapter, transport.store
    # Exercise real rich send/edit request construction, not mocked UI helpers.
    del adapter._send_rich_ui
    del adapter._edit_rich_ui_message
    adapter._peer_for = lambda chat: (types.InputPeerUser(int(chat), 0) if int(chat) > 0
                                     else types.InputPeerChannel(abs(int(chat)), 0))
    _notice_peer(adapter)
    source = _source() if origin == "group" else _source(chat_type="dm", chat_id=OWNER)
    message_id = 55 if origin == "admission" else 901
    if origin == "admission":
        await adapter._on_callback_query(_callback(f"hm:admission:allow:{target}", source))
    else:
        await command(transport, f"/allow {target}", source)

    requests = [call.args[0] for call in adapter._client.await_args_list]
    confirmation, = requests
    assert isinstance(confirmation, functions.messages.EditMessageRequest if origin == "admission"
                      else functions.messages.SendMessageRequest)
    if origin == "admission":
        assert confirmation.id == message_id
    else:
        assert confirmation.reply_to.reply_to_msg_id == 55
    assert bytes(confirmation)
    text = confirmation.message
    assert "Confirm access grant" in text and f"User {target}" in text and f"`{target}`" in text
    assert "all chats in this profile" in text
    assert "not the owner's profile or OS sudo" in text
    assert "Access granted" not in text and "separate access card" not in text
    buttons = [button for row in confirmation.reply_markup.rows for button in row.buttons]
    assert buttons[0].style.bg_success and buttons[1].style.bg_danger
    payload = _callback_payload(confirmation.reply_markup, f"hm:allow:{action}:")
    assert not store.get_access(target)["allowed"] and not _recipient_notices(adapter, target)

    event = _callback(payload, source, message_id=message_id)
    await adapter._on_callback_query(event)
    result = adapter._client.await_args.args[0]
    assert isinstance(result, functions.messages.EditMessageRequest)
    assert result.id == message_id and result.peer == confirmation.peer
    assert bytes(result) and result.reply_markup.rows == []
    assert ("Access granted" if action == "confirm" else "Access grant cancelled") in result.message
    assert store.get_access(target)["allowed"] is (action == "confirm")
    assert not store.get_access("203" if target == "201" else "201")["allowed"]
    owner_sends = [call.args[0] for call in adapter._client.await_args_list
                   if isinstance(call.args[0], functions.messages.SendMessageRequest)
                   and call.args[0].peer == confirmation.peer]
    assert len(owner_sends) == (0 if origin == "admission" else 1)
    assert len(_recipient_notices(adapter, target)) == (1 if action == "confirm" else 0)
    if action == "confirm":
        store.allow_user(Principal(OWNER), target, False)
    count = adapter._client.await_count
    await adapter._on_callback_query(_callback(payload, source, message_id=message_id))
    assert not store.get_access(target)["allowed"]  # Cancel/replayed Allow cannot regrant.
    assert adapter._client.await_count == count


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["message", "profile", "policy", "scope", "global_actor", "member", "superseded", "edit_failure"])
@pytest.mark.parametrize("action", ["confirm", "cancel"])
async def test_allow_confirmation_capability_cannot_move_between_messages_or_profiles(
    transport, monkeypatch, mismatch, action,
):
    from plugins.platforms.telegram import telegram_controls

    adapter, store = transport.adapter, transport.store
    await command(transport, "/allow 201")
    token = _confirmation_token(adapter)
    source = _source()
    home_b = transport.home / "profile-b"
    home_b.mkdir()
    (home_b / ".env").write_text("OWNER_TELEGRAM_ID=100\n")
    store_b = store_for_home(home_b)
    store_b.start_user("201")
    payload = f"hm:allow:{action}:{token}"
    if mismatch == "superseded":
        await telegram_controls.handle_owner_command(
            adapter, source, "allow", "203", confirmation_event=_callback("", source),
        )
        replacement = _callback_payload(adapter._edit_rich_ui_message.await_args.args[3], "hm:allow:confirm:")
        adapter._edit_rich_ui_message.reset_mock()
        await adapter._on_callback_query(_callback(payload, source))
        assert token not in adapter._pending_allow_requests
        assert not store.get_access("201")["allowed"] and not store.get_access("203")["allowed"]
        adapter._edit_rich_ui_message.assert_not_awaited()
        await adapter._on_callback_query(_callback(replacement, source))
        assert store.get_access("203")["allowed"] and not store.get_access("201")["allowed"]
        return
    if mismatch == "edit_failure":
        adapter._send_rich_ui.reset_mock()
        adapter._edit_rich_ui_message.side_effect = RuntimeError("edit unavailable")
        await adapter._on_callback_query(_callback(payload, source))
        assert store.get_access("201")["allowed"] is (action == "confirm")
        assert token not in adapter._pending_allow_requests
        adapter._send_rich_ui.assert_not_awaited()  # Never create a replacement result card.
        await adapter._on_callback_query(_callback(payload, source))
        assert adapter._edit_rich_ui_message.await_count == 1
        return
    with monkeypatch.context() as changed:
        if mismatch == "profile":
            changed.setattr(adapter.gateway_runner, "_resolve_profile_home_for_source", lambda _source: home_b)
        elif mismatch == "policy":
            changed.setattr(telegram_controls, "_tenant_store", lambda *_args: store_b)
        elif mismatch == "scope":
            source.chat_type = "dm"
        elif mismatch in {"global_actor", "member"}:
            source.user_id = "200" if mismatch == "global_actor" else "201"
        event = _callback(payload, source, message_id=99 if mismatch == "message" else 55)
        await adapter._on_callback_query(event)
    assert not store.get_access("201")["allowed"] and not store_b.get_access("201")["allowed"]
    assert token in adapter._pending_allow_requests
    adapter._edit_rich_ui_message.assert_not_awaited()
    assert not _recipient_notices(adapter)
    # Return A -> B -> A: rejected callbacks did not consume or mutate A's grant.
    await adapter._on_callback_query(_callback(payload, _source()))
    assert store.get_access("201")["allowed"] is (action == "confirm")
    assert not store_b.get_access("201")["allowed"]
    assert token not in adapter._pending_allow_requests
