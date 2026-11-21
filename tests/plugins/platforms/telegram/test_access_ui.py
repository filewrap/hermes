"""Allowlist rich table: owner first with disabled actions, live revoke/ban."""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent.mongo_tenants import Principal
from gateway.mongo_multiuser import store_for_home
from plugins.platforms.telegram.access_ui import allowlist_view
from plugins.platforms.telegram.telegram_controls import (
    handle_owner_callback,
    handle_owner_command,
)

OWNER = "9000"


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


def _table_rows(text):
    """Return the raw <tr>…</tr> bodies of the allowlist table, in order."""
    return re.findall(r"<tr>(.*?)</tr>", text, flags=re.DOTALL)


def _assert_no_emoji(text):
    assert not any(
        0x1F000 <= ord(char) <= 0x1FAFF or 0x2600 <= ord(char) <= 0x27BF
        for char in text
    )


def test_allowlist_owner_row_first_with_disabled_actions():
    users = [
        {"user_id": "3", "name": "Three"},
        {"user_id": "41", "name": "Forty One"},
        {"user_id": "12", "name": "Twelve"},
    ]
    text, markup = allowlist_view(
        users, 1, default_policy={}, user_policies={}, owner_id=OWNER,
    )
    assert markup is None
    _assert_no_emoji(text)
    rows = _table_rows(text)
    assert len(rows) == 5  # header + owner + 3 users
    owner_row = rows[1]
    assert "<code>9000</code>" in owner_row
    assert "Configured owner" in owner_row
    assert text.index("<code>9000</code>") < text.index("<code>3</code>")
    assert owner_row.count('type="disabled"') == 2
    assert "hm:allowlist:revoke:9000" not in text
    assert "hm:allowlist:ban:9000" not in text
    member_row = next(row for row in rows if "<code>3</code>" in row)
    assert 'data="hm:allowlist:revoke:3:1"' in member_row
    assert 'data="hm:allowlist:ban:3:1"' in member_row
    assert "<p><i>Page 1 of 1 • total 4</i></p>" in text


def test_allowlist_skips_non_numeric_ids_and_falls_back_to_id_for_missing_names():
    users = [
        {"user_id": "abc", "name": "Not Numeric"},
        {"user_id": "7"},
        {"user_id": "12", "name": "Twelve"},
    ]
    text, markup = allowlist_view(
        users, 1, default_policy={}, user_policies={}, owner_id=OWNER,
    )
    assert markup is None
    assert "Not Numeric" not in text
    assert "<code>7</code>" in text
    assert "<code>12</code>" in text


def test_allowlist_out_of_range_page_raises():
    with pytest.raises(ValueError, match="out of range"):
        allowlist_view(
            [{"user_id": "3"}], 2, default_policy={}, user_policies={}, owner_id=OWNER,
        )


@pytest.mark.asyncio
async def test_allowlist_ban_button_bans_via_real_handler(tmp_path):
    (tmp_path / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER}\n", encoding="utf-8")
    store = store_for_home(tmp_path)
    store.start_user("12345")
    store.record_started_identity("12345", "alice", "Alice")
    store.allow_user(Principal(OWNER), "12345", True)
    adapter = _adapter(tmp_path, store)
    source = _source()

    await handle_owner_command(adapter, source, "allowlist", "")
    card_text, card_markup = adapter._send_rich_ui.await_args.args[1:3]
    assert card_markup is None
    assert "hm:allowlist:ban:12345:1" in card_text
    assert "hm:allowlist:revoke:12345:1" in card_text

    event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    assert await handle_owner_callback(adapter, event, source, "hm:allowlist:ban:12345:1")
    assert store.get_access("12345")["banned"] is True
    refreshed = adapter._edit_rich_ui_message.await_args.args[2]
    assert "Banned Telegram ID 12345" in refreshed
    assert "Banned" in refreshed


@pytest.mark.asyncio
async def test_allowlist_ban_refuses_owner(tmp_path):
    (tmp_path / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER}\n", encoding="utf-8")
    store = store_for_home(tmp_path)
    adapter = _adapter(tmp_path, store)
    source = _source()
    event = SimpleNamespace(answer=AsyncMock(), message_id=77)

    assert await handle_owner_callback(adapter, event, source, "hm:allowlist:ban:9000:1")
    assert "cannot be banned" in event.answer.await_args.args[0]
    assert store.get_access("9000").get("banned") is not True
