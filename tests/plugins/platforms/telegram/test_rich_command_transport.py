"""Exercise actual native send requests, local admission and group precedence."""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from agent.mongo_tenants import Principal
from gateway.config import PlatformConfig
from gateway.mongo_multiuser import store_for_home
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings

OWNER = '100'
GROUP = '-100123'


@pytest.fixture
def rich_transport(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig(extra={'require_mention': True}))
    adapter._bot_name = 'Naina'
    adapter._bot_username = 'NainaAssistantBot'
    adapter._bot_id = 900
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda source: tmp_path,
        _profile_name_for_source=lambda _source, adapter_profile=None: adapter_profile,
    )
    adapter._source = AsyncMock(side_effect=lambda event: event.source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=800))
    adapter._input_peer_for = AsyncMock(side_effect=lambda chat: types.InputPeerUser(int(chat), 0) if int(chat)>0 else types.InputPeerChannel(abs(int(chat)), 0))
    store = store_for_home(tmp_path)
    return adapter, store, tmp_path


def incoming(text, uid=OWNER, chat=None, kind='dm'):
    source = SimpleNamespace(user_id=uid, user_name='A <b>name</b>', chat_id=chat or uid,
                             chat_type=kind, thread_id=None, is_bot=False)
    return SimpleNamespace(source=source, message=SimpleNamespace(message=text, id=44, reply_to_msg_id=None),
                           get_sender=AsyncMock(return_value=types.InputUser(int(uid), 987654)))


def rich_text(value):
    """Read literal text from native TL blocks, including nested detail blocks."""
    if isinstance(value, str):
        return value
    if isinstance(value, types.TextPlain):
        return value.text
    if isinstance(value, types.InputRichMessage):
        return " ".join(rich_text(block) for block in value.blocks)
    if isinstance(value, types.PageListItemText):
        return rich_text(value.text)
    if isinstance(value, types.PageBlockList):
        return " ".join(rich_text(item.text) for item in value.items)
    if isinstance(value, (list, tuple)):
        return " ".join(rich_text(item) for item in value)
    if hasattr(value, "to_dict"):
        return rich_text(value.to_dict())
    if isinstance(value, dict):
        return " ".join(rich_text(v) for k, v in value.items() if k != "_")
    return ""


@pytest.mark.asyncio
async def test_owner_start_sends_one_native_welcome_and_never_self_notifies(rich_transport):
    adapter, store, _home = rich_transport
    event = incoming('/start')
    await adapter._on_new_message(event)
    await adapter._on_new_message(event)
    assert adapter._client.await_count == 2
    for call in adapter._client.await_args_list:
        request = call.args[0]
        # The welcome is native Rich HTML, never the old blocks welcome: the
        # display name is escaped inside its profile button.
        assert isinstance(request.rich_message, types.InputRichMessageHTML)
        html = request.rich_message.html
        assert 'hey <tg-button type="url" url="tg://user?id=100">A &lt;b&gt;name&lt;/b&gt;</tg-button>!' in html
        assert 'A <b>name</b>' not in request.message
        assert 'Naina</tg-button>, an AI assistant.' in html
        assert '/help' in html and 'coding' in html and 'research' in html
        assert 'Telegram user' not in html
        assert request.reply_to.reply_to_msg_id == 44
    assert store.resolve_started_user(OWNER)['start_count'] == 2


@pytest.mark.asyncio
async def test_standard_telegram_start_uses_inline_native_profile_button_with_display_name(rich_transport, monkeypatch):
    adapter, _store, _home = rich_transport
    monkeypatch.setattr(adapter, "_multiuser_home", lambda source: None)
    monkeypatch.setattr(adapter, "_authorized", lambda source: True)
    monkeypatch.setattr(adapter, "_group_triggered", AsyncMock(return_value=True))
    event = incoming('/start')
    await adapter._on_new_message(event)
    request = adapter._client.await_args.args[0]
    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    assert request.reply_to.reply_to_msg_id == 44
    assert 'A &lt;b&gt;name&lt;/b&gt;' in request.message
    assert 'A <b>name</b>' not in request.message
    assert f'tg://user?id={OWNER}' in request.rich_message.html
    assert '<tg-button type="url" url="tg://user?id=100">' in request.rich_message.html


@pytest.mark.asyncio
async def test_nonowner_start_keeps_admission_and_owner_notice(rich_transport):
    adapter, store, _home = rich_transport
    await adapter._on_new_message(incoming('/start', '200'))
    assert adapter._client.await_count == 3
    welcome, reaction, notice = [call.args[0] for call in adapter._client.await_args_list]
    # The welcome is native Rich HTML with the display name escaped; the
    # owner notice is native RichMessage blocks with a real button row.
    assert isinstance(welcome.rich_message, types.InputRichMessageHTML)
    html = welcome.rich_message.html
    assert "don’t recognize you in this profile" in html
    assert 'A &lt;b&gt;name&lt;/b&gt;' in html
    assert 'A <b>name</b>' not in welcome.message
    assert 'tg://user?id=200' in html and 'tg://user?id=100' in html
    assert isinstance(reaction, functions.messages.SendReactionRequest)
    assert reaction.msg_id == 44 and reaction.reaction[0].emoticon == '🥴'
    assert isinstance(notice.rich_message, types.InputRichMessage)
    assert notice.message == 'A <b>name</b> (200) started Naina.\nActions you can take:'
    assert isinstance(notice.rich_message.blocks[0].text.texts[2], types.TextFixed)
    assert 'Actions you can take:' in rich_text(notice.rich_message)
    assert notice.peer == adapter._peer_for(OWNER)
    assert notice.reply_to is None and bytes(notice.rich_message)
    assert notice.reply_markup is None
    row = notice.rich_message.blocks[-1]
    assert isinstance(row, types.PageBlockButtonRow)
    ban, allow = row.buttons
    assert (ban.text.text, ban.type.data, ban.style.bg_danger) == ('Ban', b'hm:admission:ban:200', True)
    assert (allow.text.text, allow.type.data, allow.style.bg_success) == ('Allow', b'hm:admission:allow:200', True)
    assert welcome.reply_markup is None
    assert store.get_access('200')['allowed'] is False
    assert store.resolve_started_user('200')['start_count'] == 1


@pytest.mark.asyncio
async def test_authorized_nonowner_start_has_no_access_block_or_permission_mutation(rich_transport):
    adapter, store, _home = rich_transport
    store.start_user('200')
    store.grant_sudo(Principal(OWNER), '200', 'dm')
    await adapter._on_new_message(incoming('/start', '200'))
    request = adapter._client.await_args_list[0].args[0]
    card = request.rich_message
    # The welcome is native Rich HTML with the display name escaped inside
    # its profile button — never raw markup from a display name.
    assert isinstance(card, types.InputRichMessageHTML)
    assert '/help' in card.html and 'Naina' in card.html
    assert 'A &lt;b&gt;name&lt;/b&gt;' in request.message
    assert 'A <b>name</b>' not in request.message
    assert 'tg://user?id=200' in card.html
    assert adapter._client.await_count == 2
    assert store.access_for_scope('200', 'dm', '200')['sudo'] is True
    assert store.access_for_scope('200', 'group', GROUP)['sudo'] is False


@pytest.mark.asyncio
async def test_banned_start_never_sends_or_increments(rich_transport):
    adapter, store, _home = rich_transport
    store.ban_user(Principal(OWNER), '200', True)
    await adapter._on_new_message(incoming('/start', '200'))
    adapter._client.assert_not_awaited()
    assert store.resolve_started_user('200') is None


@pytest.mark.asyncio
async def test_group_start_never_registers_or_notifies_owner(rich_transport):
    adapter, store, _home = rich_transport
    await adapter._on_new_message(incoming('/start', '200', GROUP, 'group'))
    adapter._client.assert_awaited_once()
    assert 'private chat' in rich_text(adapter._client.await_args.args[0].rich_message)
    assert store.resolve_started_user('200') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['group', 'forum'])
@pytest.mark.parametrize('free_response', [False, True])
async def test_allowall_off_preserves_authorized_name_invocation_but_blocks_guests(rich_transport, kind, free_response):
    adapter, store, home = rich_transport
    if free_response:
        adapter.config.extra['free_response_chats'] = [GROUP]
    update_chat_settings(home, 'group', GROUP, {'trigger_mode':'trigger', 'allow_all':False})
    message = incoming('Naina hello', chat=GROUP, kind=kind)
    assert await adapter._group_triggered(message, message.source) is True
    store.allow_user(Principal(OWNER), '200', True)
    admitted = incoming('Naina hello', '200', GROUP, kind)
    assert await adapter._group_triggered(admitted, admitted.source) is True
    guest = incoming('Naina hello', '300', GROUP, kind)
    assert await adapter._group_triggered(guest, guest.source) is False
    admin = incoming('/gsettings@NainaAssistantBot', chat=GROUP, kind=kind)
    assert await adapter._group_triggered(admin, admin.source) is False
    admin_with_name = incoming('/gsettings@NainaAssistantBot Naina', chat=GROUP, kind=kind)
    assert await adapter._group_triggered(admin_with_name, admin_with_name.source) is True
    update_chat_settings(home, 'group', GROUP, {'allow_all':True})
    assert await adapter._group_triggered(message, message.source) is True


@pytest.mark.asyncio
async def test_username_trigger_accepts_hidden_telegram_mention_entity(rich_transport):
    adapter, _store, home = rich_transport
    update_chat_settings(home, 'group', GROUP, {'trigger_mode': 'tag', 'allow_all': True})

    class MessageEntityMentionName:
        user_id = 900

    event = incoming('Hey there', chat=GROUP, kind='group')
    event.message.entities = [MessageEntityMentionName()]
    assert await adapter._group_triggered(event, event.source) is True


@pytest.mark.asyncio
async def test_singular_gsetting_only_explains_rename_without_mutation(rich_transport):
    adapter, store, _home = rich_transport
    await adapter._on_new_message(incoming('/gsetting enabled=false', chat=GROUP, kind='group'))
    adapter._client.assert_awaited_once()
    assert 'renamed' in rich_text(adapter._client.await_args.args[0].rich_message)
    assert store.get_group_settings(Principal(OWNER), GROUP) is None


@pytest.mark.parametrize('allowed,owner', [(True, True), (True, False), (False, False)])
def test_start_is_simple_html_and_command_specific_for_every_state(allowed, owner):
    from plugins.platforms.telegram.start_ui import start_html_view, UNRECOGNIZED
    name = 'Keii <script>Admin</script> & "Full Name" *bold* [link](https://evil.invalid) 🧪'
    html = start_html_view(bot_name='Naina', allowed=allowed, user_id=200, user_name=name,
                           owner_id=100 if not allowed else None)
    assert '<tg-button type="url" url="tg://user?id=200">' in html
    assert name not in html
    assert 'Keii &lt;script&gt;Admin&lt;/script&gt;' in html
    assert 'PageBlock' not in html and 'TextButton' not in html
    if allowed:
        assert html.startswith('<p>hey ')
        assert "I'm Naina, an AI assistant." in html
        assert '<code>/help</code>' in html and 'coding' in html and 'research' in html
        assert '<li>' not in html and html.count('<p>') == 4
        assert '<i>A little clarity' in html and ' · ' in html
        assert '<details>' not in html
        assert UNRECOGNIZED not in html
    else:
        assert UNRECOGNIZED in html
        assert 'AgainOwner' not in html and 'MY OWNER' not in html
        for forbidden in ('hello', 'btw', "I'm Naina", 'introduced', '/help'):
            assert forbidden.lower() not in html.lower()
        assert '<details>' in html and 'Want to meet me?' in html
        assert 'Ask to be added to this profile' in html
        assert '<tg-button type="url" style="primary" url="tg://user?id=100">Contact owner</tg-button>' in html


def test_start_renders_bot_profile_button_when_bot_id_known():
    from plugins.platforms.telegram.start_ui import start_html_view
    html = start_html_view(bot_name='Naina', allowed=True, user_id=200, user_name='Keii',
                           bot_id=900)
    assert '<tg-button type="url" url="tg://user?id=900">Naina</tg-button>' in html
    assert "I'm <tg-button" in html
    assert '<tg-button type="url" url="tg://user?id=200">Keii</tg-button>' in html


def test_unlisted_start_requires_owner_id_instead_of_link_fallback():
    from plugins.platforms.telegram.start_ui import start_html_view
    with pytest.raises(ValueError, match='owner'):
        start_html_view(bot_name='Naina', allowed=False, user_id=200, user_name='Keii')
    with pytest.raises(ValueError, match='numeric Telegram user ID'):
        start_html_view(bot_name='Naina', allowed=True, user_id='not-a-user')

@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['hello', 'Naina hello', '/help', '/settings', '/allow 300', '/allowall'])
async def test_unlisted_private_messages_only_react_without_admission(rich_transport, text):
    adapter, store, _home = rich_transport
    await adapter._on_new_message(incoming(text, '200'))
    adapter._client.assert_awaited_once()
    request = adapter._client.await_args.args[0]
    assert isinstance(request, functions.messages.SendReactionRequest)
    assert request.msg_id == 44 and request.reaction[0].emoticon == '🥴'
    assert store.get_access('200')['allowed'] is False
    assert store.resolve_started_user('200') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['hello', '/start', '/settings'])
async def test_unsupported_unauthorized_reaction_is_safe_and_sends_no_fallback(rich_transport, text):
    adapter, store, _home = rich_transport
    async def reject_reaction(request):
        if isinstance(request, functions.messages.SendReactionRequest):
            raise RuntimeError('REACTION_INVALID')
        return SimpleNamespace(id=800)
    adapter._client.side_effect = reject_reaction
    await adapter._on_new_message(incoming(text, '200'))
    assert store.get_access('200')['allowed'] is False
    sent = [c.args[0] for c in adapter._client.await_args_list if isinstance(c.args[0], functions.messages.SendMessageRequest)]
    assert len(sent) == (2 if text == '/start' else 0)
    assert all('pending approval' not in rich_text(r.rich_message).lower() for r in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize('banned', [False, True])
async def test_revoked_or_banned_user_cannot_regain_access_with_start_or_trigger(rich_transport, banned):
    adapter, store, _home = rich_transport
    store.allow_user(Principal(OWNER), '200', True)
    store.allow_user(Principal(OWNER), '200', False)
    if banned:
        store.ban_user(Principal(OWNER), '200', True)
    for text in ('/start', 'Naina hello', '/allowall', '/help'):
        await adapter._on_new_message(incoming(text, '200'))
        assert store.get_access('200')['allowed'] is False
        assert store.access_for_scope('200', 'dm', '200')['sudo'] is False
    if banned:
        adapter._client.assert_not_awaited()
    else:
        card = adapter._client.await_args_list[0].args[0].rich_message
        assert "don’t recognize you in this profile" in rich_text(card)


@pytest.mark.asyncio
async def test_start_uses_native_sender_profile_instead_of_name(rich_transport):
    adapter, _store, _home = rich_transport
    adapter._multiuser_home = lambda source: None
    adapter._authorized = lambda source: True
    adapter._group_triggered = AsyncMock(return_value=True)
    event = incoming('/start')
    event.get_sender = AsyncMock(return_value=types.InputUser(100, 123456))
    await adapter._on_new_message(event)
    card = adapter._client.await_args.args[0].rich_message
    assert isinstance(card, types.InputRichMessageHTML)
    assert 'tg://user?id=100' in card.html
    assert 'A &lt;b&gt;name&lt;/b&gt;' in card.html
    assert 'A <b>name</b>' not in card.html
    assert 'Keii <Admin>' not in card.html


@pytest.mark.asyncio
@pytest.mark.parametrize('uid,allowed', [(OWNER, True), ('200', True)])
async def test_start_badrequest_never_downgrades_rich_welcome(
        rich_transport, caplog, uid, allowed):
    from telethon.errors import BadRequestError

    adapter, store, _home = rich_transport
    if allowed and uid != OWNER:
        store.start_user(uid)
        store.grant_sudo(Principal(OWNER), uid, 'dm')
    rejected = []

    async def reject_rich(request):
        if isinstance(request, functions.messages.SendMessageRequest) and str(request.peer.user_id) == uid:
            rejected.append(request)
            raise BadRequestError(request, 'RICH_MESSAGE_BLOCKS_INVALID')
        return SimpleNamespace(id=801)

    adapter._client.side_effect = reject_rich
    event = incoming('/start', uid)
    event.source.user_name = 'Keii 🧪 <b>Full & Name</b>'
    await adapter._on_new_message(event)
    assert len(rejected) == 1
    assert rejected[0].rich_message is not None
    assert rejected[0].reply_to.reply_to_msg_id == 44
    assert isinstance(rejected[0].rich_message, types.InputRichMessageHTML)
    html = rejected[0].rich_message.html
    assert 'Keii 🧪 &lt;b&gt;Full &amp; Name&lt;/b&gt;' in html
    assert event.source.user_name not in rejected[0].message
    assert ('<code>/help</code>' in html) is allowed
    assert f'tg://user?id={uid}' in html
    assert 'tg-button' in html
    sent_messages = [call.args[0] for call in adapter._client.await_args_list
                     if isinstance(call.args[0], functions.messages.SendMessageRequest)]
    assert sent_messages and all(request.rich_message is not None for request in sent_messages)
    assert store.access_for_scope(uid, 'dm', uid)['allowed'] is allowed
    assert store.resolve_started_user(uid)['start_count'] == (2 if allowed and uid != OWNER else 1)
    reactions = [c.args[0] for c in adapter._client.await_args_list
                 if isinstance(c.args[0], functions.messages.SendReactionRequest)]
    assert len(reactions) == (0 if allowed else 1)
    assert all(r.reaction[0].emoticon == '🥴' for r in reactions)
    assert 'RICH_MESSAGE_BLOCKS_INVALID' in caplog.text
    assert event.source.user_name not in caplog.text and 'SendMessageRequest(' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['legacy_header', 'timeout', 'private_badrequest'])
async def test_start_uses_profile_button_and_logs_no_request_or_private_reason(rich_transport, caplog, failure):
    from telethon.errors import BadRequestError

    adapter, _store, _home = rich_transport
    calls = []
    async def transport(request):
        calls.append(request)
        assert request.message and 'Private Name' not in request.message
        assert request.rich_message is not None
        if failure == 'legacy_header':
            assert isinstance(request.rich_message, types.InputRichMessageHTML)
            assert 'tg-button' in request.rich_message.html
            assert "I'm Naina" in request.rich_message.html
            raise BadRequestError(request, 'RICH_MESSAGE_BLOCKS_INVALID')
        elif failure == 'timeout':
            raise TimeoutError('private name and bot token must never be logged')
        else:
            raise BadRequestError(request, 'private name https://example.invalid/secret?token=abc')
    adapter._request_with_bounded_retry = transport
    from plugins.platforms.telegram.start_ui import start_html_view
    card = start_html_view(bot_name='Naina', allowed=True, user_id=100, user_name='Keii')
    result = await adapter._send_start_welcome(OWNER, card)
    assert not result.success
    assert len(calls) == 1
    assert calls[0].rich_message.html == card and calls[0].reply_markup is None
    assert 'private name' not in caplog.text and 'token=' not in caplog.text
    assert 'Private Name' not in caplog.text and 'SendMessageRequest(' not in caplog.text

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['badrequest', 'timeout'])
async def test_nested_profile_button_never_downgrades_to_link_text_or_keyboard(rich_transport, caplog, failure):
    from telethon.errors import BadRequestError
    from plugins.platforms.telegram.start_ui import start_html_view
    adapter, _store, _home = rich_transport
    async def reject(request):
        if failure == 'timeout':
            raise TimeoutError('private detail')
        raise BadRequestError(request, 'RICH_MESSAGE_BLOCKS_INVALID')
    adapter._request_with_bounded_retry = AsyncMock(side_effect=reject)
    card = start_html_view(bot_name='Naina', allowed=False, user_id=200, owner_id=100)
    result = await adapter._send_start_welcome('200', card, reply_to=44)
    assert not result.success
    adapter._request_with_bounded_retry.assert_awaited_once()
    request = adapter._request_with_bounded_retry.await_args.args[0]
    assert request.rich_message.html == card and request.reply_markup is None
    assert not request.entities
    assert 'Private Name' not in request.message and 'href' not in request.message
    assert 'private detail' not in caplog.text


@pytest.mark.asyncio
async def test_owner_start_with_dead_store_operation_gets_allowed_welcome(rich_transport, monkeypatch):
    """Mongo down mid-/start (store constructs, start_user raises): the owner
    is known from the profile .env, so /start answers with the allowed rich
    welcome — never "I don't recognize you, ask the owner"."""
    from agent.mongo_tenants import TenantStoreError

    adapter, store, _home = rich_transport

    class FailingStore:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def start_user(self, uid):
            raise TenantStoreError("Tenant storage operation failed")

    monkeypatch.setattr("gateway.mongo_multiuser.store_for_home", lambda _home: FailingStore(store))
    await adapter._on_new_message(incoming('/start'))
    requests = [call.args[0] for call in adapter._client.await_args_list]
    texts = " ".join(rich_text(getattr(req, "rich_message", None) or getattr(req, "message", "")) for req in requests)
    assert "an AI assistant" in texts
    assert "I don’t recognize you" not in texts


@pytest.mark.asyncio
async def test_owner_start_with_unconstructable_store_gets_allowed_welcome(rich_transport, monkeypatch):
    """Mongo down at connect (the store cannot even be constructed): /start
    from the owner must still answer with the allowed welcome instead of
    vanishing into the branch-wide deny."""
    from agent.mongo_tenants import TenantStoreError

    adapter, _store, _home = rich_transport

    def dead_store(_home):
        raise TenantStoreError("Cannot initialize tenant storage")

    monkeypatch.setattr("gateway.mongo_multiuser.store_for_home", dead_store)
    await adapter._on_new_message(incoming('/start'))
    requests = [call.args[0] for call in adapter._client.await_args_list]
    texts = " ".join(rich_text(getattr(req, "rich_message", None) or getattr(req, "message", "")) for req in requests)
    assert "an AI assistant" in texts
    assert "I don’t recognize you" not in texts


@pytest.mark.asyncio
async def test_nonowner_start_with_dead_store_still_gets_unlisted_wording(rich_transport, monkeypatch):
    """Failing closed for strangers is unchanged: a non-owner still gets the
    unlisted wording (and no allowed welcome) when a store operation fails,
    and nothing at all when the store cannot even be constructed."""
    from agent.mongo_tenants import TenantStoreError

    adapter, store, _home = rich_transport

    class FailingStore:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def start_user(self, uid):
            raise TenantStoreError("Tenant storage operation failed")

    monkeypatch.setattr("gateway.mongo_multiuser.store_for_home", lambda _home: FailingStore(store))
    await adapter._on_new_message(incoming('/start', uid='200'))
    requests = [call.args[0] for call in adapter._client.await_args_list]
    texts = " ".join(rich_text(getattr(req, "rich_message", None) or getattr(req, "message", "")) for req in requests)
    assert "I don’t recognize you" in texts
    assert "an AI assistant" not in texts

    def dead_store(_home):
        raise TenantStoreError("Cannot initialize tenant storage")

    monkeypatch.setattr("gateway.mongo_multiuser.store_for_home", dead_store)
    await adapter._on_new_message(incoming('/start', uid='200'))
    assert adapter._client.await_count == len(requests)
