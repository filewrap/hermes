from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent.mongo_tenants import Principal
from gateway.mongo_multiuser import store_for_home
from plugins.platforms.telegram.access_ui import allowlist_view
from plugins.platforms.telegram.telegram_chat_settings import settings_view
from plugins.platforms.telegram.telegram_controls import handle_owner_callback, handle_owner_command

OWNER = "9000"


def _buttons(markup):
    return [button for row in getattr(markup, "rows", ()) for button in getattr(row, "buttons", ())]


def _adapter(home, store, *, owner=True):
    adapter = SimpleNamespace()
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: home)
    adapter._multiuser_home = lambda _source: home
    adapter._is_owner_user = lambda user_id, source=None: bool(owner and str(user_id) == OWNER)
    adapter._is_explicitly_allowed = lambda _source: False
    adapter._bot_name = "Hermes"
    adapter._send_rich_ui = AsyncMock()
    adapter._edit_rich_ui_message = AsyncMock()
    adapter._client = SimpleNamespace()
    return adapter


def _source(user_id=OWNER, *, chat_type="dm", chat_id=None):
    return SimpleNamespace(
        user_id=str(user_id), chat_type=chat_type,
        chat_id=str(chat_id if chat_id is not None else user_id),
    )


@pytest.mark.asyncio
async def test_allowlist_command_shows_sorted_numeric_ids_and_persists_owner_policy(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER}\n", encoding="utf-8")
    store = store_for_home(tmp_path)
    for uid in (
        "12", "3", "41", "25", "9", "33", "17", "51", "62", "72", "81",
        "92", "103", "114", "125", "136", "147", "158", "169", "170", "181",
    ):
        store.start_user(uid)
        store.record_started_identity(uid, f"user{uid}", f"User {uid}")
        store.allow_user(Principal(OWNER), uid, True)
    adapter = _adapter(tmp_path, store)
    source = _source()

    await handle_owner_command(adapter, source, "allowlist", "")
    text, markup = adapter._send_rich_ui.await_args.args[1:3]
    assert markup is None
    assert "Page 1 of 2" in text
    assert "<code>3</code>" in text
    assert text.index("<code>3</code>") < text.index("<code>9</code>") < text.index("<code>12</code>")
    assert "hm:allowlist:page:2" in text

    await handle_owner_command(adapter, source, "allowlist", 'policy 12 {"denied_tools":["terminal"]}')
    from agent.tenant_policy import load_policy
    assert "terminal" in load_policy(tmp_path, "12")["denied_tools"]
    assert "Policy saved" in adapter._send_rich_ui.await_args.args[1]
    assert "configured owner bypasses" in adapter._send_rich_ui.await_args.args[1]


@pytest.mark.asyncio
async def test_allowlist_policy_requires_owner_private_chat(tmp_path):
    (tmp_path / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER}\n", encoding="utf-8")
    store = store_for_home(tmp_path)
    adapter = _adapter(tmp_path, store)
    await handle_owner_command(adapter, _source(OWNER, chat_id="-1001"), "allowlist", "")
    assert "private chat" in adapter._send_rich_ui.await_args.args[1]


@pytest.mark.asyncio
async def test_allowlist_page_callback_rechecks_owner_and_private_chat():
    event = SimpleNamespace(answer=AsyncMock(), message_id=4)
    source = _source(OWNER)
    adapter = _adapter(None, None, owner=False)
    adapter._is_explicitly_allowed = lambda _source: True
    assert await handle_owner_callback(adapter, event, source, "hm:allowlist:page:1")
    assert "Only the configured owner" in event.answer.await_args.args[0]
    adapter._edit_rich_ui_message.assert_not_awaited()


def test_group_settings_do_not_publish_roster_grant_or_revoke_controls():
    text, markup = settings_view(
        "group", {"trigger_word": "naina", "allowed_users": {"12345": {"name": "Private person"}}},
    )
    assert "12345" not in text and "Private person" not in text
    assert "/allow " not in text and "Remove Private" not in text
    assert "global grants" in text and "direct-message access" in text
    assert all(not getattr(button.type, "data", b"").startswith(b"hm:settings:remove:") for button in _buttons(markup))


@pytest.mark.asyncio
async def test_allowlist_revoke_requires_owner_private_confirmation_and_never_bans(tmp_path):
    (tmp_path / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER}\n", encoding="utf-8")
    store = store_for_home(tmp_path)
    store.start_user("12345")
    store.record_started_identity("12345", "alice", "Alice")
    store.allow_user(Principal(OWNER), "12345", True)
    adapter = _adapter(tmp_path, store)
    source = _source()

    await handle_owner_command(adapter, source, "allowlist", "")
    card_text, card_markup = adapter._send_rich_ui.await_args.args[1:3]
    assert "/allow numeric_id" in card_text
    assert "send <code>/start</code> privately" in card_text
    assert card_markup is None
    revoke_data = "hm:allowlist:revoke:12345:1"
    assert revoke_data in card_text

    event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    assert await handle_owner_callback(adapter, event, source, revoke_data)
    assert store.get_access("12345")["allowed"] is True
    prompt, prompt_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    assert "Confirm global access revocation" in prompt
    assert "does not ban" in prompt
    confirm_data = next(
        button.type.data.decode() for button in _buttons(prompt_markup)
        if b":confirm:" in button.type.data
    )

    # Even a valid pending token is unusable outside the owner's private chat.
    group_source = _source(OWNER, chat_type="group", chat_id="-1001")
    denied_event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    await handle_owner_callback(adapter, denied_event, group_source, confirm_data)
    assert store.get_access("12345")["allowed"] is True
    assert denied_event.answer.await_args.kwargs["alert"] is True

    confirm_event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    await handle_owner_callback(adapter, confirm_event, source, confirm_data)
    assert store.get_access("12345")["allowed"] is False
    assert store.get_access("12345")["banned"] is False
    refreshed = adapter._edit_rich_ui_message.await_args.args[2]
    assert "Revoked global access" in refreshed


@pytest.mark.asyncio
async def test_allowlist_revoke_cancel_is_non_destructive_and_message_bound(tmp_path):
    (tmp_path / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER}\n", encoding="utf-8")
    store = store_for_home(tmp_path)
    store.start_user("12345")
    store.record_started_identity("12345", "alice", "Alice")
    store.allow_user(Principal(OWNER), "12345", True)
    adapter = _adapter(tmp_path, store)
    source = _source()
    event = SimpleNamespace(answer=AsyncMock(), message_id=88)

    await handle_owner_callback(adapter, event, source, "hm:allowlist:revoke:12345:1")
    prompt_markup = adapter._edit_rich_ui_message.await_args.args[3]
    cancel_data = next(
        button.type.data.decode() for button in _buttons(prompt_markup)
        if b":cancel:" in button.type.data
    )
    wrong_message = SimpleNamespace(answer=AsyncMock(), message_id=89)
    await handle_owner_callback(adapter, wrong_message, source, cancel_data)
    assert store.get_access("12345")["allowed"] is True
    assert wrong_message.answer.await_args.kwargs["alert"] is True

    cancel_event = SimpleNamespace(answer=AsyncMock(), message_id=88)
    await handle_owner_callback(adapter, cancel_event, source, cancel_data)
    assert store.get_access("12345")["allowed"] is True
    assert store.get_access("12345")["banned"] is False
