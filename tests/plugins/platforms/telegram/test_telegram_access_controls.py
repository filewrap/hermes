from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.local_tenants import LocalTenantStore
from agent.mongo_tenants import Principal
from plugins.platforms.telegram import telegram_controls
from plugins.platforms.telegram.telegram_chat_settings import allowlist_view, settings_view


OWNER = "9000"


def _buttons(markup):
    return [button for row in markup.rows for button in row.buttons]


def _owner_adapter(home, *, actor=OWNER, allowed=False):
    return SimpleNamespace(
        gateway_runner=SimpleNamespace(_resolve_profile_home_for_source=lambda _source: home),
        _is_owner_user=lambda user_id, source=None: str(user_id) == OWNER,
        _is_explicitly_allowed=lambda _source: allowed,
        _send_rich_ui=AsyncMock(),
    )


def test_pending_start_view_uses_native_buttons_without_reply_markup():
    from plugins.platforms.telegram.access_ui import pending_start_view

    rich, reply_markup = pending_start_view(
        {"id": "200", "name": "<Alex & Sam>"}, bot_name="Hermes",
    )

    assert isinstance(rich, str) and reply_markup is None
    assert "&lt;Alex &amp; Sam&gt;" in rich and "<Alex & Sam>" not in rich
    assert "<code>200</code>" in rich and "started Hermes." in rich
    assert 'data="hm:admission:ban:200"' in rich
    assert 'data="hm:admission:allow:200"' in rich
    assert 'style="danger"' in rich and 'style="success"' in rich
    assert "<tg-button-row>" in rich

    with pytest.raises(ValueError, match="numeric Telegram user ID"):
        pending_start_view({"id": "@alex", "name": "Alex"})


def test_allowlist_view_uses_explicit_one_based_page_index_and_native_rich_blocks():
    from telethon import types
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    users = [
        {"user_id": str(i), "name": f"User {i}", "username": f"user_{i}"}
        for i in range(1, 26)
    ]
    text, markup = allowlist_view(users, 2)

    assert "Page 2 of 2 · 25 globally allowed account(s)." in text
    assert "User 21" in text and "User 25" in text
    assert "User 1</td>" not in text
    rich = input_rich_message(text)
    assert isinstance(rich, types.InputRichMessageHTML)
    # input_rich_message canonicalizes a standalone page paragraph into the
    # final italic footer, so the parsed HTML is not byte-identical to the
    # view text — but the table content must survive the rewrite untouched.
    assert "Page 2 of 2 · 25 globally allowed account(s)." not in rich.html
    assert rich.html.endswith("<p><i>Page 2 of 2 • total 25</i></p>")
    assert "User 21" in rich.html and "User 25" in rich.html
    assert "User 1</td>" not in rich.html
    assert [button.type.data for button in _buttons(markup)] == [b"hm:allowlist:page:1"]
    assert _buttons(markup)[0].text == "Previous"

    with pytest.raises(ValueError, match="out of range"):
        allowlist_view(users, 3)


def test_allowlist_rows_escape_untrusted_identity_and_show_ban_override():
    text, _markup = allowlist_view([{
        "user_id": '42<&"', "name": '<Alice & Bob>', "username": 'a<&"', "banned": True,
    }], 1)
    assert "&lt;Alice &amp; Bob&gt;" in text
    assert "42&lt;&amp;&quot;" in text
    assert '@a&lt;&amp;&quot;' in text
    assert "Banned; grant retained" in text
    assert "<Alice" not in text


@pytest.mark.asyncio
async def test_allowlist_command_is_private_owner_only_and_uses_requested_page(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    store = LocalTenantStore(home, OWNER)
    for user_id in range(1, 26):
        store.start_user(str(user_id))
        store.record_started_identity(str(user_id), None, f"User {user_id:03d}")
        store.allow_user(Principal(OWNER), str(user_id), True)
    monkeypatch.setattr(telegram_controls, "_tenant_store", lambda _adapter, _source: store)
    adapter = _owner_adapter(home)
    source = SimpleNamespace(user_id=OWNER, chat_id=OWNER, chat_type="dm")

    assert await telegram_controls.handle_owner_command(adapter, source, "allowlist", "2")
    text, markup = adapter._send_rich_ui.await_args.args[1:3]
    assert "Page 2 of 2" in text
    assert "User 021" in text
    assert markup is None
    assert "hm:allowlist:page:1" in text

    group_source = SimpleNamespace(user_id=OWNER, chat_id="-10055", chat_type="group")
    adapter._send_rich_ui.reset_mock()
    assert await telegram_controls.handle_owner_command(adapter, group_source, "allowlist", "")
    assert "private chat" in adapter._send_rich_ui.await_args.args[1]

    nonowner = _owner_adapter(home, actor="42", allowed=True)
    nonowner._is_owner_user = lambda user_id, source=None: False
    nonowner_source = SimpleNamespace(user_id="42", chat_id="42", chat_type="dm")
    assert await telegram_controls.handle_owner_command(nonowner, nonowner_source, "allowlist", "")
    # Owner-only command from an admitted non-owner: silently denied.
    nonowner._send_rich_ui.assert_not_awaited()


@pytest.mark.asyncio
async def test_disallow_revokes_only_exact_numeric_global_grant(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    store = LocalTenantStore(home, OWNER)
    store.allow_user(Principal(OWNER), "12001", True)
    store.allow_user(Principal(OWNER), "12002", True)
    monkeypatch.setattr(telegram_controls, "_tenant_store", lambda _adapter, _source: store)
    adapter = _owner_adapter(home)
    source = SimpleNamespace(user_id=OWNER, chat_id=OWNER, chat_type="dm")

    await telegram_controls.handle_owner_command(adapter, source, "disallow", "12001")
    assert store.get_access("12001")["allowed"] is False
    assert store.get_access("12002")["allowed"] is True
    assert "12001" in adapter._send_rich_ui.await_args.args[1]

    adapter._send_rich_ui.reset_mock()
    await telegram_controls.handle_owner_command(adapter, source, "disallow", "@12002")
    assert store.get_access("12002")["allowed"] is True
    assert "numeric user id" in adapter._send_rich_ui.await_args.args[1]

    adapter._send_rich_ui.reset_mock()
    nonowner = _owner_adapter(home, actor="12002", allowed=True)
    nonowner._is_owner_user = lambda user_id, source=None: False
    await telegram_controls.handle_owner_command(
        nonowner, SimpleNamespace(user_id="12002", chat_id="12002", chat_type="dm"),
        "disallow", "12002",
    )
    assert store.get_access("12002")["allowed"] is True
    # Owner-only command from an admitted non-owner: silently denied.
    nonowner._send_rich_ui.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('name_source', ['started', 'profile', 'telegram', 'unknown', 'failed_lookup'])
async def test_disallow_name_and_content_without_redundant_footer(tmp_path, monkeypatch, name_source):
    from html import escape
    from xml.etree import ElementTree
    from telethon import types
    from plugins.platforms.telegram.mtproto_rich import send_request

    store = LocalTenantStore(tmp_path, OWNER)
    name = 'Mike <Full & Name> **literal**'
    if name_source == 'started':
        store.start_user('12001')
        store.record_started_identity('12001', 'mike_test', name)
    store.allow_user(Principal(OWNER), '12001', True)
    if name_source == 'profile':
        store._profiles.update_one({'user_id': '12001'}, {'$set': {'name': name}})
    monkeypatch.setattr(telegram_controls, '_tenant_store', lambda *_args: store)
    adapter = _owner_adapter(tmp_path)
    lookup = AsyncMock(return_value=SimpleNamespace(id=12001, first_name='Mike', last_name='Full Name'))
    if name_source == 'failed_lookup':
        lookup.side_effect = RuntimeError('private RPC payload')
    elif name_source == 'unknown':
        lookup.return_value = SimpleNamespace(id=12001, first_name=None, last_name=None)
    adapter._client = SimpleNamespace(get_entity=lookup)
    source = SimpleNamespace(user_id=OWNER, chat_id=OWNER, chat_type='dm')
    await telegram_controls.handle_owner_command(adapter, source, 'disallow', '12001')

    assert not store.get_access('12001')['allowed']
    html = adapter._send_rich_ui.await_args.args[1]
    request = send_request(types.InputPeerUser(int(OWNER), 0), html)
    assert isinstance(request.rich_message, types.InputRichMessageHTML) and bytes(request)
    body = ElementTree.fromstring('<body>' + request.rich_message.html + '</body>')
    assert "<hr" not in html and "Page 1 of 1" not in html and "Total 1" not in html
    assert ["".join(node.itertext()) for node in list(body)] == [
        "Global access revoked",
        f"{name if name_source in {'started', 'profile'} else 'Mike Full Name' if name_source == 'telegram' else 'Telegram user 12001'} no longer has global access to this profile.",
        "Group guest access, when enabled by that group's Allow all setting, is separate.",
    ]
    assert 'no longer has global access' in html and 'Group guest access' in html
    if name_source in {'started', 'profile'}:
        assert f'<b>{escape(name)}</b>' in html and '<Full' not in html
        lookup.assert_not_awaited()
    elif name_source == 'telegram':
        assert '<b>Mike Full Name</b>' in html
        lookup.assert_awaited_once_with(12001)
    else:
        assert '<b>Telegram user 12001</b>' in html
    assert store.access_for_scope('12001', 'dm', '12001')['sudo'] is False


def test_group_controls_color_selected_states_without_checkmarks():
    text, markup = settings_view(
        "group", {"trigger_word": "naina", "trigger_mode": "tag", "allowed_users": {}},
        bot_username="hermes_bot",
    )
    buttons = {button.text: button for button in _buttons(markup)}
    assert "Allow all · ON" not in buttons and "Allow all · OFF" not in buttons
    assert buttons["Username"].style.bg_success is True
    assert not buttons["Name trigger"].style.bg_success
    assert not any("✓" in button.text or "✔" in button.text for button in _buttons(markup))
    assert "Custom trigger word: <b>naina</b>" in text
    assert "The invocation mode never grants access" in text


def test_native_menu_includes_global_allowlist_controls():
    from hermes_cli.commands_platforms import telegram_bot_commands

    names = {name for name, _description in telegram_bot_commands(include_plugins=False)}
    assert {"allowlist", "disallow"} <= names
