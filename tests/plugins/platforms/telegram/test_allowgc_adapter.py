"""Behaviour contracts for /allowgc in the Telegram adapter.

Confirm creates the shared group profile and edits the same card to the
enabled view; cancel deletes both messages; turn-off preserves the profile
for seven days; expired confirmations edit the card; non-owners are silently
denied; the trigger toggle only flips when the group's /trigger word is set; when
/trigger is off it alerts and does not change; members take the normal
turn path, not the tenant path.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from gateway.allowgc import (
    create_group_profile,
    is_active_for_chat,
    load_state,
    resolve_home,
)
from gateway.config import PlatformConfig
from gateway.mongo_multiuser import store_for_home
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

OWNER = "100"
MEMBER = "201"
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


def _callback(data, source, *, message_id=55):
    return SimpleNamespace(
        source=source,
        data=data.encode(),
        message_id=message_id,
        answer=AsyncMock(),
    )


def _buttons(markup):
    """(label, callback data, green?) for every inline-keyboard button."""
    out = []
    for row in getattr(markup, "rows", ()) or ():
        for button in getattr(row, "buttons", ()) or ():
            data = getattr(getattr(button, "type", None), "data", b"")
            out.append((
                button.text,
                data.decode() if isinstance(data, bytes) else str(data),
                bool(getattr(getattr(button, "style", None), "bg_success", False)),
            ))
    return out


def _payload(markup, prefix):
    for _label, data, _green in _buttons(markup):
        if data.startswith(prefix):
            return data
    raise AssertionError(f"No {prefix!r} callback button in markup")


_PILL_RE = re.compile(
    r'<tg-button type="callback_data"(?: style="(?P<style>[^"]+)")?'
    r' data="(?P<data>[^"]+)">(?P<label>[^<]+)</tg-button>'
)


def _pills(text):
    """(label, callback data, style-or-None) for every in-body callback pill."""
    return [
        (match.group("label"), match.group("data"), match.group("style"))
        for match in _PILL_RE.finditer(text)
    ]


def _confirm_token(adapter):
    payload = _payload(adapter._send_rich_ui.await_args.args[2], "hm:allowgc:confirm:")
    match = re.fullmatch(r"hm:allowgc:confirm:([0-9a-f]{32})", payload)
    assert match, payload
    return match.group(1)


async def _reshow_allowgc(transport, *, message_id=88):
    """Repeat /allowgc while active; return (text, markup, token).

    The repeat sends a new card where the command was sent, so the render
    comes from the new send, not an in-place edit.
    """
    adapter = transport.adapter
    await command(transport, "/allowgc", message_id=message_id)
    text, markup = adapter._send_rich_ui.await_args.args[1:3]
    payload = _payload(markup, "hm:allowgc:turnoff:")
    match = re.fullmatch(r"hm:allowgc:turnoff:([0-9a-f]{32})", payload)
    assert match, payload
    return text, markup, match.group(1)


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


async def enable_allowgc(transport):
    """Run /allowgc + Confirm; return the enabled card's token."""
    await command(transport, "/allowgc", message_id=77)
    token = _confirm_token(transport.adapter)
    await transport.adapter._on_callback_query(
        _callback(f"hm:allowgc:confirm:{token}", _source()))
    assert is_active_for_chat(GROUP)
    markup = transport.adapter._edit_rich_ui_message.await_args.args[3]
    payload = _payload(markup, "hm:allowgc:turnoff:")
    match = re.fullmatch(r"hm:allowgc:turnoff:([0-9a-f]{32})", payload)
    assert match, payload
    return match.group(1)


def _no_emoji(text):
    assert not re.search('[\U0001F000-\U0001FAFF\u2600-\u27BF]', text)


@pytest.mark.asyncio
async def test_allowgc_confirm_creates_profile_and_edits_card_to_enabled(transport):
    adapter = transport.adapter
    await command(transport, "/allowgc", message_id=77)

    adapter._send_rich_ui.assert_awaited_once()
    text, markup = adapter._send_rich_ui.await_args.args[1:3]
    assert "Allowgc" in text and f"Chat {GROUP}" in text and GROUP in text
    assert 'data="badge:beta"' in text
    assert "<details>" not in text
    # Italic footer, no divider above it.
    assert text.strip().endswith("<p><i>Click confirm to enable it in this group.</i></p>")
    assert "<hr" not in text
    _no_emoji(text)
    # The 4 toggles are inline-keyboard buttons — green (success) while on —
    # in two rows of two, ahead of Confirm/Cancel. No toggle pill in the
    # card text: the only in-body pill is the beta badge in the heading.
    assert "hm:allowgc:toggle:" not in text
    buttons = _buttons(markup)
    assert [label for label, _data, _green in buttons[:4]] == [
        "Tools: On", "Web: On", "Shell: On", "Trigger: On",
    ]
    assert all(green for _label, _data, green in buttons[:4])
    assert all(len(data.encode()) <= 64 for _label, data, _green in buttons)
    assert ("Confirm", f"hm:allowgc:confirm:{_confirm_token(adapter)}", True) in buttons
    token = _confirm_token(adapter)
    assert adapter._pending_allowgc_requests[token]["chat_id"] == GROUP
    assert adapter._pending_allowgc_requests[token]["user_message_id"] == "77"

    await adapter._on_callback_query(_callback(f"hm:allowgc:confirm:{token}", _source()))

    gc_home = resolve_home(GROUP, reap=False)
    assert gc_home is not None
    state = load_state(gc_home)
    assert state["enabled"] is True and state["owner_id"] == OWNER
    assert state["chat_name"] == f"Chat {GROUP}"
    assert state["toggles"] == {"tools": True, "web": True, "shell": True, "trigger": True}
    assert state["card_message_id"] == "55"  # the Confirm card's message
    assert store_for_home(gc_home).owner_user_id == OWNER
    # Same message edited; no new message sent.
    assert adapter._send_rich_ui.await_count == 1
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_text, edited_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    assert "can now run Hermes" in edited_text
    assert f"Chat {GROUP}" in edited_text and GROUP in edited_text
    assert "belongs to another chat" not in edited_text
    _no_emoji(edited_text)
    edited_buttons = _buttons(edited_markup)
    assert any(label == "Turn off" and data.startswith("hm:allowgc:turnoff:")
               for label, data, _green in edited_buttons)
    assert not any(data.startswith("hm:allowgc:confirm:")
                   for _label, data, _green in edited_buttons)
    # The enabled card shows the state as word badges, not a full-line
    # status badge: Status: + green "enabled", one green/red on/off badge
    # per access, and the default footer. No Save button by default.
    assert "<p>Status:" in edited_text
    assert '<tg-button type="disabled" style="success">enabled</tg-button>' in edited_text
    assert edited_text.count('<tg-button type="disabled" style="success">on</tg-button>') == 4
    assert "Click the buttons to turn access on or off." in edited_text
    assert not any(label == "Save" for label, _data, _green in edited_buttons)
    # The confirm token is consumed: replaying it edits the card to expired.
    adapter._edit_rich_ui_message.reset_mock()
    await adapter._on_callback_query(_callback(f"hm:allowgc:confirm:{token}", _source()))
    assert "expired" in adapter._edit_rich_ui_message.await_args.args[2].lower()


@pytest.mark.asyncio
async def test_allowgc_toggles_are_keyboard_buttons_green_when_on(transport):
    """Toggles are inline-keyboard buttons, green when on, unstyled when off.

    The card body carries badges only on the five state words (Status,
    Tools, Web, Shell, Trigger) — the beta badge in the heading, the
    "enabled" word badge, one on/off word badge per access. No toggle
    pill lives in the card text. The keyboard carries the 4 toggles in two
    rows of two, then the action buttons (Confirm/Cancel, then Turn off);
    Turn off stays danger, not green.
    """
    adapter = transport.adapter
    await command(transport, "/allowgc", message_id=77)

    text, markup = adapter._send_rich_ui.await_args.args[1:3]
    # The beta badge is still the one in-body pill, blue.
    badge = [pill for pill in _pills(text)
             if pill[1] == "badge:beta"]
    assert badge == [("beta", "badge:beta", "primary")]
    assert "hm:allowgc:toggle:" not in text
    assert text.strip().endswith("<p><i>Click confirm to enable it in this group.</i></p>")
    rows = [[button.text for button in row.buttons] for row in markup.rows]
    assert rows == [
        ["Tools: On", "Web: On"],
        ["Shell: On", "Trigger: On"],
        ["Confirm", "Cancel"],
    ]
    buttons = _buttons(markup)
    assert [label for label, _data, _green in buttons[:4]] == [
        "Tools: On", "Web: On", "Shell: On", "Trigger: On",
    ]
    assert all(green for _label, _data, green in buttons[:4])
    assert all(len(data.encode()) <= 64 for _label, data, _green in buttons)
    assert buttons[4][2] is True  # Confirm stays green
    token = _confirm_token(adapter)

    await adapter._on_callback_query(_callback(f"hm:allowgc:confirm:{token}", _source()))

    edited_text, edited_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    assert "hm:allowgc:toggle:" not in edited_text
    # State-word badges still in the body, toggles now keyboard buttons.
    assert '<tg-button type="disabled" style="success">enabled</tg-button>' in edited_text
    assert edited_text.count('<tg-button type="disabled" style="success">on</tg-button>') == 4
    rows = [[button.text for button in row.buttons] for row in edited_markup.rows]
    assert rows == [
        ["Tools: On", "Web: On"],
        ["Shell: On", "Trigger: On"],
        ["Turn off"],
    ]
    buttons = _buttons(edited_markup)
    assert all(green for _label, _data, green in buttons[:4])
    assert buttons[4][2] is False  # Turn off stays danger, not green

    # Flipping Web off un-greens only that keyboard button: unstyled,
    # still tappable; its state-word badge flips to red "off".
    web_payload = _payload(edited_markup, "hm:allowgc:toggle:web:")
    await adapter._on_callback_query(_callback(web_payload, _source()))
    flipped_text, flipped_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    web_button = [item for item in _buttons(flipped_markup)
                  if item[1].startswith("hm:allowgc:toggle:web:")]
    assert web_button == [("Web: Off", web_button[0][1], False)]
    assert [green for _label, _data, green in _buttons(flipped_markup)[:4]] == [
        True, False, True, True,
    ]
    assert '<tg-button type="disabled" style="danger">off</tg-button>' in flipped_text


@pytest.mark.asyncio
async def test_allowgc_repeat_while_active_shows_current_state_not_fresh_confirmation(transport):
    adapter = transport.adapter
    old_token = await enable_allowgc(transport)
    adapter._send_rich_ui.reset_mock()
    adapter._edit_rich_ui_message.reset_mock()

    text, markup, token = await _reshow_allowgc(transport, message_id=88)

    # The repeat sends a new card where the command was sent: the stored
    # card is not edited in place, and the old card's token is retired.
    adapter._send_rich_ui.assert_awaited_once()
    adapter._edit_rich_ui_message.assert_not_awaited()
    assert old_token not in adapter._pending_allowgc_requests
    assert adapter._pending_allowgc_requests[token]["message_id"] == "55"  # the new card
    assert load_state(resolve_home(GROUP, reap=False))["card_message_id"] == "55"
    assert "can now run Hermes" in text
    assert "<p>Status:" in text
    assert '<tg-button type="disabled" style="success">enabled</tg-button>' in text
    assert text.count('<tg-button type="disabled" style="success">on</tg-button>') == 4
    assert "Click the buttons to turn access on or off." in text
    assert "Allowgc" in text and 'data="badge:beta"' in text
    _no_emoji(text)
    buttons = _buttons(markup)
    assert [label for label, _data, _green in buttons[:4]] == [
        "Tools: On", "Web: On", "Shell: On", "Trigger: On",
    ]
    assert all(green for _label, _data, green in buttons[:4])
    assert not any(data.startswith("hm:allowgc:confirm:") for _label, data, _green in buttons)
    assert not any(label == "Confirm" for label, _data, _green in buttons)
    assert not any(label == "Save" for label, _data, _green in buttons)
    assert any(label == "Turn off" and data.startswith("hm:allowgc:turnoff:")
               for label, data, _green in buttons)
    assert all(len(data.encode()) <= 64 for _label, data, _green in buttons)
    request = adapter._pending_allowgc_requests[token]
    assert request["confirmed"] is True
    assert request["chat_id"] == GROUP
    assert request["user_message_id"] == "88"


@pytest.mark.asyncio
async def test_allowgc_repeat_sends_new_card_and_retires_old_token(transport):
    from gateway.allowgc import load_state

    adapter = transport.adapter
    old_token = await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    assert load_state(gc_home)["card_message_id"] == "55"
    adapter._send_rich_ui.reset_mock()
    adapter._edit_rich_ui_message.reset_mock()
    adapter._edit_rich_ui_message.side_effect = Exception("message gone")

    await command(transport, "/allowgc", message_id=88)

    # The repeat never edits the stored card: a new card is sent instead,
    # its id is stored, and the old card's token is retired.
    adapter._edit_rich_ui_message.assert_not_awaited()
    adapter._send_rich_ui.assert_awaited_once()
    text, _markup = adapter._send_rich_ui.await_args.args[1:3]
    assert "can now run Hermes" in text
    assert load_state(gc_home)["card_message_id"] == "55"  # receipt id from the mock
    assert old_token not in adapter._pending_allowgc_requests


@pytest.mark.asyncio
async def test_allowgc_second_repeat_supersedes_first(transport):
    adapter = transport.adapter
    await enable_allowgc(transport)
    _first_text, first_markup, first_token = await _reshow_allowgc(transport, message_id=88)
    adapter._send_rich_ui.reset_mock()

    _text, _markup, second_token = await _reshow_allowgc(transport, message_id=89)

    assert first_token not in adapter._pending_allowgc_requests
    assert second_token in adapter._pending_allowgc_requests
    # A tap on the superseded card answers as expired.
    web_payload = _payload(first_markup, "hm:allowgc:toggle:web:")
    callback = _callback(web_payload, _source())
    await adapter._on_callback_query(callback)
    callback.answer.assert_awaited_once()
    assert "expired" in callback.answer.await_args.args[0].lower()
    assert callback.answer.await_args.kwargs.get("alert") is True


@pytest.mark.asyncio
async def test_allowgc_repeat_without_stored_card_sends_new_and_stores_id(transport):
    from gateway.allowgc import load_state

    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    assert load_state(gc_home)["card_message_id"] is None
    adapter._send_rich_ui.reset_mock()

    await command(transport, "/allowgc", message_id=88)

    adapter._send_rich_ui.assert_awaited_once()
    adapter._edit_rich_ui_message.assert_not_awaited()
    text, _markup = adapter._send_rich_ui.await_args.args[1:3]
    assert "can now run Hermes" in text
    assert load_state(gc_home)["card_message_id"] == "55"


@pytest.mark.asyncio
async def test_allowgc_repeat_card_carries_live_toggle_values(transport):
    from gateway.allowgc import set_toggle

    adapter = transport.adapter
    await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    set_toggle(gc_home, "web", False)
    set_toggle(gc_home, "shell", False)
    adapter._send_rich_ui.reset_mock()

    text, markup, _token = await _reshow_allowgc(transport, message_id=88)

    assert [label for label, _data, _green in _buttons(markup)[:4]] == [
        "Tools: On", "Web: Off", "Shell: Off", "Trigger: On",
    ]
    assert text.count('<tg-button type="disabled" style="danger">off</tg-button>') == 2
    assert text.count('<tg-button type="disabled" style="success">on</tg-button>') == 2
    assert '<tg-button type="disabled" style="success">enabled</tg-button>' in text


@pytest.mark.asyncio
async def test_allowgc_reshown_toggle_stages_until_save_persists(transport):
    from gateway.allowgc import load_state

    adapter = transport.adapter
    await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    _text, markup, token = await _reshow_allowgc(transport, message_id=88)
    adapter._edit_rich_ui_message.reset_mock()

    web_payload = _payload(markup, "hm:allowgc:toggle:web:")
    await adapter._on_callback_query(_callback(web_payload, _source()))
    # Staged only: the group profile state is untouched by the toggle tap.
    assert load_state(gc_home)["toggles"]["web"] is True
    assert adapter._pending_allowgc_requests[token]["toggles"]["web"] is False
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_markup = adapter._edit_rich_ui_message.await_args.args[3]
    web_button = [item for item in _buttons(edited_markup)
                  if item[1].startswith("hm:allowgc:toggle:web:")]
    assert web_button == [("Web: Off", web_button[0][1], False)]
    assert any(label == "Save" for label, _data, _green in _buttons(edited_markup))

    adapter._edit_rich_ui_message.reset_mock()
    await adapter._on_callback_query(_callback(f"hm:allowgc:save:{token}", _source()))
    # Save persists the staged toggles to the group profile state.
    assert load_state(gc_home)["toggles"]["web"] is False
    assert token in adapter._pending_allowgc_requests  # the card stays editable
    adapter._edit_rich_ui_message.assert_awaited_once()
    saved_text, saved_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    assert "<p>Status:" in saved_text
    assert '<tg-button type="disabled" style="success">enabled</tg-button>' in saved_text
    assert '<tg-button type="disabled" style="danger">off</tg-button>' in saved_text
    assert [label for label, _data, _green in _buttons(saved_markup)[:4]] == [
        "Tools: On", "Web: Off", "Shell: On", "Trigger: On",
    ]
    # Save applied the staged change: the button and the save line are
    # gone, and the card is back to its default footer.
    assert not any(label == "Save" for label, _data, _green in _buttons(saved_markup))
    assert "Click save to save the current changes." not in saved_text
    assert "Click the buttons to turn access on or off." in saved_text


@pytest.mark.asyncio
async def test_allowgc_confirm_card_stages_toggles_until_save(transport):
    from gateway.allowgc import load_state

    adapter = transport.adapter
    token = await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    adapter._edit_rich_ui_message.reset_mock()

    # The post-Confirm card stages like the repeat card: flipping Web does
    # not touch state, but the Save button and the save line appear.
    await adapter._on_callback_query(_callback(f"hm:allowgc:toggle:web:{token}", _source()))
    assert load_state(gc_home)["toggles"]["web"] is True
    assert adapter._pending_allowgc_requests[token]["toggles"]["web"] is False
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_text, edited_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    assert any(label == "Save" for label, _data, _green in _buttons(edited_markup))
    assert "Click save to save the current changes." in edited_text

    # Save persists it, and the card goes back to its default shape.
    adapter._edit_rich_ui_message.reset_mock()
    await adapter._on_callback_query(_callback(f"hm:allowgc:save:{token}", _source()))
    assert load_state(gc_home)["toggles"]["web"] is False
    adapter._edit_rich_ui_message.assert_awaited_once()
    saved_text, saved_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    assert not any(label == "Save" for label, _data, _green in _buttons(saved_markup))
    assert "Click save to save the current changes." not in saved_text
    assert "Click the buttons to turn access on or off." in saved_text


@pytest.mark.asyncio
async def test_allowgc_identical_rerender_skips_edit():
    from plugins.platforms.telegram.mtproto_rich import inline_markup
    from plugins.platforms.telegram.telegram_controls import (
        _allowgc_edit_if_changed,
        _allowgc_render_key,
    )

    adapter = SimpleNamespace(_edit_rich_ui_message=AsyncMock())
    event = SimpleNamespace(message_id=55)
    source = _source()
    token = "a" * 32
    text = "<p>Status: x</p>"
    markup = inline_markup([[("Turn off", f"hm:allowgc:turnoff:{token}", "danger")]])
    request = {"rendered": _allowgc_render_key(text, markup)}

    assert await _allowgc_edit_if_changed(adapter, event, source, request, text, markup) is False
    adapter._edit_rich_ui_message.assert_not_awaited()

    assert await _allowgc_edit_if_changed(
        adapter, event, source, request, text + "!", markup) is True
    adapter._edit_rich_ui_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_allowgc_save_with_no_changes_is_a_quiet_noop(transport):
    from gateway.allowgc import load_state

    adapter = transport.adapter
    await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    before = load_state(gc_home)["toggles"]
    _text, _markup, token = await _reshow_allowgc(transport, message_id=88)
    adapter._edit_rich_ui_message.reset_mock()

    callback = _callback(f"hm:allowgc:save:{token}", _source())
    await adapter._on_callback_query(callback)

    assert load_state(gc_home)["toggles"] == before
    assert token in adapter._pending_allowgc_requests
    # No staged change: nothing to save, and the message is not re-edited.
    adapter._edit_rich_ui_message.assert_not_awaited()
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_allowgc_expired_save_edits_expired_card(transport):
    from gateway.allowgc import load_state

    adapter = transport.adapter
    await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    _text, markup, token = await _reshow_allowgc(transport, message_id=88)
    web_payload = _payload(markup, "hm:allowgc:toggle:web:")
    await adapter._on_callback_query(_callback(web_payload, _source()))
    adapter._pending_allowgc_requests[token]["created"] -= 6 * 60 * 60 + 1
    adapter._edit_rich_ui_message.reset_mock()

    await adapter._on_callback_query(_callback(f"hm:allowgc:save:{token}", _source()))

    assert load_state(gc_home)["toggles"]["web"] is True  # staged change never saved
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_text = adapter._edit_rich_ui_message.await_args.args[2]
    assert "expired" in edited_text.lower()
    assert "Send /allowgc again" in edited_text


@pytest.mark.asyncio
async def test_allowgc_mismatched_save_toasts_only_and_token_survives(transport):
    from gateway.allowgc import load_state

    adapter = transport.adapter
    await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    _text, markup, token = await _reshow_allowgc(transport, message_id=88)
    web_payload = _payload(markup, "hm:allowgc:toggle:web:")
    await adapter._on_callback_query(_callback(web_payload, _source()))
    adapter._edit_rich_ui_message.reset_mock()

    callback = _callback(f"hm:allowgc:save:{token}", _source(chat_id=OTHER_GROUP))
    await adapter._on_callback_query(callback)

    callback.answer.assert_awaited_once()
    assert "does not match" in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs.get("alert") is True
    assert token in adapter._pending_allowgc_requests
    assert load_state(gc_home)["toggles"]["web"] is True
    assert adapter._edit_rich_ui_message.await_count == 0

    # The correct owner can still save from the right card.
    await adapter._on_callback_query(_callback(f"hm:allowgc:save:{token}", _source()))
    assert load_state(gc_home)["toggles"]["web"] is False


@pytest.mark.asyncio
async def test_allowgc_save_persists_only_toggles_flipped_on_that_card(transport):
    from gateway.allowgc import load_state, set_toggle

    adapter = transport.adapter
    await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    _text, markup_b, token_b = await _reshow_allowgc(transport, message_id=88)

    # Flip Web on the re-shown card (staged); change Shell directly in the
    # profile state, as if another actor changed it after this card was
    # shown. A second card can no longer stay live — a repeat supersedes
    # the previous card — so the concurrent change is simulated directly.
    web_payload = _payload(markup_b, "hm:allowgc:toggle:web:")
    await adapter._on_callback_query(_callback(web_payload, _source()))
    assert load_state(gc_home)["toggles"]["web"] is True  # nothing persisted yet
    set_toggle(gc_home, "shell", False)
    assert load_state(gc_home)["toggles"]["shell"] is False

    # Save on the re-shown card must persist Web (flipped there) without
    # clobbering Shell (changed elsewhere after this card was shown).
    adapter._edit_rich_ui_message.reset_mock()
    await adapter._on_callback_query(_callback(f"hm:allowgc:save:{token_b}", _source()))
    assert load_state(gc_home)["toggles"] == {
        "tools": True, "web": False, "shell": False, "trigger": True,
    }
    saved_text, saved_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    assert saved_text.count('<tg-button type="disabled" style="danger">off</tg-button>') == 2
    assert [label for label, _data, _green in _buttons(saved_markup)[:4]] == [
        "Tools: On", "Web: Off", "Shell: Off", "Trigger: On",
    ]
    assert not any(label == "Save" for label, _data, _green in _buttons(saved_markup))


@pytest.mark.asyncio
async def test_allowgc_save_after_turnoff_retires_card_without_saving(transport):
    from gateway.allowgc import disable_group_profile, load_state

    adapter = transport.adapter
    await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    _text, markup, token_b = await _reshow_allowgc(transport, message_id=88)
    web_payload = _payload(markup, "hm:allowgc:toggle:web:")
    await adapter._on_callback_query(_callback(web_payload, _source()))
    assert adapter._pending_allowgc_requests[token_b]["toggles"]["web"] is False
    # The profile goes away out from under the live card (turned off
    # elsewhere): the next save must retire the card without saving.
    disable_group_profile(GROUP)
    assert not is_active_for_chat(GROUP)
    adapter._edit_rich_ui_message.reset_mock()

    callback = _callback(f"hm:allowgc:save:{token_b}", _source())
    await adapter._on_callback_query(callback)

    callback.answer.assert_awaited_once()
    assert "Allowgc is off" in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs.get("alert") is True
    state = load_state(gc_home)
    assert state["enabled"] is False and state["toggles"]["web"] is True  # never saved
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_text = adapter._edit_rich_ui_message.await_args.args[2]
    assert "preserved for 7 days" in edited_text
    edited_markup = adapter._edit_rich_ui_message.await_args.args[3]
    assert _payload(edited_markup, "hm:allowgc:turnon:")


@pytest.mark.asyncio
async def test_allowgc_turnon_reenables_all_on_and_edits_to_enabled(transport):
    from gateway.allowgc import load_state, set_toggle

    adapter = transport.adapter
    token = await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    set_toggle(gc_home, "web", False)  # preserved toggles must NOT carry over
    adapter._edit_rich_ui_message.reset_mock()
    await adapter._on_callback_query(_callback(f"hm:allowgc:turnoff:{token}", _source()))
    assert not is_active_for_chat(GROUP)
    assert load_state(gc_home)["toggles"]["web"] is False

    turned_off_markup = adapter._edit_rich_ui_message.await_args.args[3]
    turnon_payload = _payload(turned_off_markup, "hm:allowgc:turnon:")
    match = re.fullmatch(r"hm:allowgc:turnon:([0-9a-f]{32})", turnon_payload)
    assert match, turnon_payload
    turnon_token = match.group(1)
    assert turnon_token in adapter._pending_allowgc_requests
    adapter._edit_rich_ui_message.reset_mock()

    await adapter._on_callback_query(_callback(turnon_payload, _source()))

    assert is_active_for_chat(GROUP)
    assert load_state(gc_home)["toggles"] == {
        "tools": True, "web": True, "shell": True, "trigger": True,
    }
    # The Turn on token is consumed: replaying it edits the card to expired.
    assert turnon_token not in adapter._pending_allowgc_requests
    # Same message edited into the enabled card; no new message sent.
    adapter._send_rich_ui.assert_awaited_once()
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_text, edited_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    assert "can now run Hermes" in edited_text
    assert '<tg-button type="disabled" style="success">enabled</tg-button>' in edited_text
    assert edited_text.count('<tg-button type="disabled" style="success">on</tg-button>') == 4
    assert not any(label == "Save" for label, _data, _green in _buttons(edited_markup))
    assert any(label == "Turn off" for label, _data, _green in _buttons(edited_markup))
    # The enabled card gets a fresh staged token for its toggles.
    new_token = _payload(edited_markup, "hm:allowgc:turnoff:")
    match = re.fullmatch(r"hm:allowgc:turnoff:([0-9a-f]{32})", new_token)
    assert match and match.group(1) in adapter._pending_allowgc_requests
    adapter._edit_rich_ui_message.reset_mock()
    await adapter._on_callback_query(_callback(turnon_payload, _source()))
    assert "expired" in adapter._edit_rich_ui_message.await_args.args[2].lower()


@pytest.mark.asyncio
async def test_allowgc_reshown_toggle_after_turnoff_retires_card(transport):
    from gateway.allowgc import disable_group_profile, load_state

    adapter = transport.adapter
    await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    _text, markup, _token_b = await _reshow_allowgc(transport, message_id=88)
    # The profile goes away out from under the live card (turned off
    # elsewhere): a toggle on it must retire the card, not stage.
    disable_group_profile(GROUP)
    adapter._edit_rich_ui_message.reset_mock()

    web_payload = _payload(markup, "hm:allowgc:toggle:web:")
    callback = _callback(web_payload, _source())
    await adapter._on_callback_query(callback)

    callback.answer.assert_awaited_once()
    assert "Allowgc is off" in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs.get("alert") is True
    assert load_state(gc_home)["toggles"]["web"] is True
    adapter._edit_rich_ui_message.assert_awaited_once()
    assert "preserved for 7 days" in adapter._edit_rich_ui_message.await_args.args[2]
    assert _payload(adapter._edit_rich_ui_message.await_args.args[3], "hm:allowgc:turnon:")


@pytest.mark.asyncio
async def test_allowgc_reenable_keeps_preserved_toggles(transport):
    from gateway.allowgc import set_toggle

    adapter = transport.adapter
    token_a = await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    set_toggle(gc_home, "web", False)
    await adapter._on_callback_query(_callback(f"hm:allowgc:turnoff:{token_a}", _source()))
    assert not is_active_for_chat(GROUP)
    adapter._send_rich_ui.reset_mock()

    # Repeat /allowgc is a fresh confirmation again — but seeded from the
    # preserved toggles, not the defaults.
    await command(transport, "/allowgc", message_id=88)
    _text, markup = adapter._send_rich_ui.await_args.args[1:3]
    assert [label for label, _data, _green in _buttons(markup)[:4]] == [
        "Tools: On", "Web: Off", "Shell: On", "Trigger: On",
    ]
    assert any(label == "Confirm" for label, _data, _green in _buttons(markup))
    assert not any(label == "Save" for label, _data, _green in _buttons(markup))
    token = _confirm_token(adapter)

    await adapter._on_callback_query(_callback(f"hm:allowgc:confirm:{token}", _source()))

    assert is_active_for_chat(GROUP)
    assert load_state(gc_home)["toggles"] == {
        "tools": True, "web": False, "shell": True, "trigger": True,
    }


@pytest.mark.asyncio
async def test_allowgc_repeat_reaps_expired_preservation(transport):
    from datetime import UTC, datetime, timedelta

    from gateway.allowgc import save_state

    adapter = transport.adapter
    token_a = await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    await adapter._on_callback_query(_callback(f"hm:allowgc:turnoff:{token_a}", _source()))
    state = load_state(gc_home)
    state["disabled_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    save_state(gc_home, state)
    adapter._send_rich_ui.reset_mock()

    await command(transport, "/allowgc", message_id=88)

    assert not gc_home.exists()  # past the 7-day preservation: reaped on lookup
    _text, markup = adapter._send_rich_ui.await_args.args[1:3]
    assert [label for label, _data, _green in _buttons(markup)[:4]] == [
        "Tools: On", "Web: On", "Shell: On", "Trigger: On",
    ]


@pytest.mark.asyncio
async def test_allowgc_cancel_deletes_both_messages(transport):
    adapter = transport.adapter
    await command(transport, "/allowgc", message_id=77)
    token = _confirm_token(adapter)

    await adapter._on_callback_query(_callback(f"hm:allowgc:cancel:{token}", _source()))

    assert not is_active_for_chat(GROUP)
    assert token not in adapter._pending_allowgc_requests
    assert adapter._send_rich_ui.await_count == 1
    adapter._client.delete_messages.assert_awaited_once()
    args, kwargs = adapter._client.delete_messages.await_args.args, adapter._client.delete_messages.await_args.kwargs
    assert args[1] == [55, 77]
    assert kwargs.get("revoke") is True


@pytest.mark.asyncio
async def test_allowgc_turnoff_preserves_seven_days_and_edits_preservation_line(transport):
    adapter = transport.adapter
    token = await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)
    assert gc_home is not None
    adapter._edit_rich_ui_message.reset_mock()

    await adapter._on_callback_query(_callback(f"hm:allowgc:turnoff:{token}", _source()))

    assert gc_home.exists()
    state = load_state(gc_home)
    assert state["enabled"] is False
    assert state["disabled_at"]
    assert not is_active_for_chat(GROUP)
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_text = adapter._edit_rich_ui_message.await_args.args[2]
    assert "preserved for 7 days" in edited_text
    assert "send /allowgc again within 7 days" in edited_text
    assert f"Chat {GROUP}" in edited_text and GROUP in edited_text
    assert "<p><b>Allowgc</b>" in edited_text and 'data="badge:beta"' in edited_text
    assert '<tg-button type="disabled" style="danger">off</tg-button>' in edited_text
    assert "Click turn on to enable it again." in edited_text


@pytest.mark.asyncio
async def test_allowgc_turnoff_edit_carries_turn_on_button(transport):
    adapter = transport.adapter
    token = await enable_allowgc(transport)
    adapter._edit_rich_ui_message.reset_mock()

    await adapter._on_callback_query(_callback(f"hm:allowgc:turnoff:{token}", _source()))

    assert not is_active_for_chat(GROUP)
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_text, edited_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    assert "preserved for 7 days" in edited_text
    # The turned-off card keeps exactly one button: green Turn on. The
    # consumed keyboard is replaced by it, never cleared with an empty
    # ReplyInlineMarkup.
    buttons = _buttons(edited_markup)
    assert len(buttons) == 1
    label, data, green = buttons[0]
    assert label == "Turn on" and green is True
    assert re.fullmatch(r"hm:allowgc:turnon:[0-9a-f]{32}", data)


@pytest.mark.asyncio
async def test_edit_or_send_text_only_edit_omits_reply_markup():
    import struct

    from plugins.platforms.telegram.telegram_controls import _edit_or_send

    sent = []
    adapter = SimpleNamespace(
        _client=AsyncMock(side_effect=lambda request: sent.append(request)),
        _peer_for=lambda chat_id: types.InputPeerSelf(),
    )

    await _edit_or_send(adapter, SimpleNamespace(message_id=55), _source(),
                        "Allowgc is off.", edit_only=True)

    assert len(sent) == 1
    request = sent[0]
    assert request.reply_markup is None
    flags = struct.unpack("<i", bytes(request)[4:8])[0]
    assert flags & 4 == 0  # the reply_markup flag bit is absent: clears the keyboard


@pytest.mark.asyncio
async def test_allowgc_expired_confirm_edits_expired_card(transport):
    adapter = transport.adapter
    await command(transport, "/allowgc", message_id=77)
    token = _confirm_token(adapter)
    adapter._pending_allowgc_requests[token]["created"] -= 6 * 60 * 60 + 1
    adapter._send_rich_ui.reset_mock()
    adapter._edit_rich_ui_message.reset_mock()

    await adapter._on_callback_query(_callback(f"hm:allowgc:confirm:{token}", _source()))

    assert not is_active_for_chat(GROUP)
    adapter._send_rich_ui.assert_not_awaited()
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_text = adapter._edit_rich_ui_message.await_args.args[2]
    assert "expired" in edited_text.lower()
    assert "Send /allowgc again" in edited_text
    assert "belongs to another chat" not in edited_text


@pytest.mark.asyncio
async def test_allowgc_expired_card_edit_carries_no_markup(transport):
    adapter = transport.adapter
    await command(transport, "/allowgc", message_id=77)
    token = _confirm_token(adapter)
    adapter._pending_allowgc_requests[token]["created"] -= 6 * 60 * 60 + 1
    adapter._edit_rich_ui_message.reset_mock()

    await adapter._on_callback_query(_callback(f"hm:allowgc:confirm:{token}", _source()))

    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_text, edited_markup = adapter._edit_rich_ui_message.await_args.args[2:4]
    assert "expired" in edited_text.lower()
    # Same crash as the turn-off edit: the expired card must be edited with
    # no reply_markup, never an empty ReplyInlineMarkup.
    assert edited_markup is None


@pytest.mark.asyncio
async def test_allowgc_mismatched_live_token_survives_for_correct_retry(transport):
    adapter = transport.adapter
    await command(transport, "/allowgc", message_id=77)
    token = _confirm_token(adapter)

    wrong = _source(chat_id=OTHER_GROUP)
    callback = _callback(f"hm:allowgc:confirm:{token}", wrong)
    await adapter._on_callback_query(callback)
    callback.answer.assert_awaited_once()
    assert "does not match" in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs.get("alert") is True
    assert token in adapter._pending_allowgc_requests
    assert not is_active_for_chat(GROUP)
    # The mismatch answers a toast and edits nothing.
    assert adapter._edit_rich_ui_message.await_count == 0

    await adapter._on_callback_query(_callback(f"hm:allowgc:confirm:{token}", _source()))
    assert is_active_for_chat(GROUP)


@pytest.mark.asyncio
async def test_allowgc_nonowner_is_silently_denied(transport):
    from agent.mongo_tenants import Principal

    adapter = transport.adapter
    # Unadmitted member: silent before any card exists.
    await command(transport, "/allowgc", _source(MEMBER), message_id=78)
    adapter._send_rich_ui.assert_not_awaited()
    adapter.send.assert_not_awaited()
    assert getattr(adapter, "_pending_allowgc_requests", {}) == {}
    transport.tenant_handler.assert_not_awaited()

    # Admitted non-owner (launch grant): still silent, no card.
    transport.store.allow_user(Principal(OWNER), MEMBER, True)
    await command(transport, "/allowgc", _source(MEMBER), message_id=79)
    adapter._send_rich_ui.assert_not_awaited()
    adapter.send.assert_not_awaited()
    assert getattr(adapter, "_pending_allowgc_requests", {}) == {}

    # And a non-owner tap on the owner's card changes nothing.
    await command(transport, "/allowgc", message_id=77)
    token = _confirm_token(adapter)
    callback = _callback(f"hm:allowgc:confirm:{token}", _source(MEMBER))
    await adapter._on_callback_query(callback)
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get("alert") is True
    assert token in adapter._pending_allowgc_requests
    assert not is_active_for_chat(GROUP)
    adapter._edit_rich_ui_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowgc_in_dm_answers_owner_with_group_only_reply(transport):
    adapter = transport.adapter
    await command(transport, "/allowgc", _source(chat_type="dm", chat_id=OWNER), message_id=99)

    adapter._send_rich_ui.assert_awaited_once()
    text = adapter._send_rich_ui.await_args.args[1]
    assert "only works in a group" in text
    assert not is_active_for_chat(GROUP)
    assert resolve_home(GROUP, reap=False) is None
    assert getattr(adapter, "_pending_allowgc_requests", {}) == {}


@pytest.mark.asyncio
async def test_allowgc_in_dm_silently_ignores_nonowner(transport):
    from agent.mongo_tenants import Principal

    adapter = transport.adapter
    # Unadmitted member: silent before any card exists.
    await command(transport, "/allowgc", _source(MEMBER, chat_type="dm", chat_id=MEMBER), message_id=99)
    adapter._send_rich_ui.assert_not_awaited()
    adapter.send.assert_not_awaited()
    assert getattr(adapter, "_pending_allowgc_requests", {}) == {}

    # Admitted non-owner: still silent, no reply.
    transport.store.allow_user(Principal(OWNER), MEMBER, True)
    await command(transport, "/allowgc", _source(MEMBER, chat_type="dm", chat_id=MEMBER), message_id=100)
    adapter._send_rich_ui.assert_not_awaited()
    adapter.send.assert_not_awaited()
    assert getattr(adapter, "_pending_allowgc_requests", {}) == {}


@pytest.mark.asyncio
async def test_allowgc_trigger_toggle_alerts_when_trigger_word_off(transport):
    adapter = transport.adapter
    await command(transport, "/allowgc", message_id=77)
    token = _confirm_token(adapter)
    markup = adapter._send_rich_ui.await_args.args[2]
    trigger_payload = _payload(markup, "hm:allowgc:toggle:trigger:")

    # No trigger word set in this group: /trigger is off.
    callback = _callback(trigger_payload, _source())
    await adapter._on_callback_query(callback)

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get("alert") is True
    assert adapter._pending_allowgc_requests[token]["toggles"]["trigger"] is True
    adapter._edit_rich_ui_message.assert_not_awaited()

    # Any other toggle flips and re-renders with its new colour.
    web_payload = _payload(markup, "hm:allowgc:toggle:web:")
    await adapter._on_callback_query(_callback(web_payload, _source()))
    assert adapter._pending_allowgc_requests[token]["toggles"]["web"] is False
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_markup = adapter._edit_rich_ui_message.await_args.args[3]
    web_button = [item for item in _buttons(edited_markup) if item[1].startswith("hm:allowgc:toggle:web:")]
    assert web_button == [("Web: Off", web_button[0][1], False)]


@pytest.mark.asyncio
async def test_allowgc_trigger_toggle_flips_when_trigger_word_set(transport):
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings

    update_chat_settings(transport.home, "group", GROUP, {"trigger_word": "heybot"})
    adapter = transport.adapter
    await command(transport, "/allowgc", message_id=77)
    token = _confirm_token(adapter)
    markup = adapter._send_rich_ui.await_args.args[2]
    trigger_payload = _payload(markup, "hm:allowgc:toggle:trigger:")

    callback = _callback(trigger_payload, _source())
    await adapter._on_callback_query(callback)

    callback.answer.assert_awaited_once()
    assert adapter._pending_allowgc_requests[token]["toggles"]["trigger"] is False
    adapter._edit_rich_ui_message.assert_awaited_once()
    edited_markup = adapter._edit_rich_ui_message.await_args.args[3]
    trigger_button = [item for item in _buttons(edited_markup) if item[1].startswith("hm:allowgc:toggle:trigger:")]
    assert trigger_button == [("Trigger: Off", trigger_button[0][1], False)]

    # Tapping again flips it back on, green.
    await adapter._on_callback_query(_callback(trigger_payload, _source()))
    assert adapter._pending_allowgc_requests[token]["toggles"]["trigger"] is True


@pytest.mark.asyncio
async def test_allowgc_confirm_carries_trigger_word_into_group_profile(transport):
    from plugins.platforms.telegram.telegram_chat_settings import (
        load_chat_settings,
        update_chat_settings,
    )

    update_chat_settings(transport.home, "group", GROUP, {"trigger_word": "heybot"})
    await command(transport, "/allowgc", message_id=77)
    token = _confirm_token(transport.adapter)
    await transport.adapter._on_callback_query(_callback(f"hm:allowgc:confirm:{token}", _source()))

    gc_home = resolve_home(GROUP, reap=False)
    assert load_chat_settings(gc_home, "group", GROUP).get("trigger_word") == "heybot"


@pytest.mark.asyncio
async def test_allowgc_badge_tap_is_toast_only_and_keeps_card(transport):
    adapter = transport.adapter
    await command(transport, "/allowgc", message_id=77)
    token = _confirm_token(adapter)

    callback = _callback("hm:allowgc:badge:beta", _source())
    await adapter._on_callback_query(callback)

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.args[0] == "beta function — enjoy"
    assert callback.answer.await_args.kwargs.get("alert") is False
    assert token in adapter._pending_allowgc_requests
    adapter._edit_rich_ui_message.assert_not_awaited()
    adapter._send_rich_ui.assert_awaited_once()


def test_allowgc_views_serialize_as_native_rich_html():
    from plugins.platforms.telegram.access_ui import (
        allowgc_confirmation_view,
        allowgc_enabled_view,
        allowgc_expired_view,
        allowgc_imout_confirm_view,
        allowgc_turned_off_view,
    )
    from plugins.platforms.telegram.mtproto_rich import edit_request

    token = "a" * 32
    toggles = {"tools": True, "web": False, "shell": True, "trigger": True}
    for text, markup in (
        allowgc_confirmation_view("Group <name>", GROUP, toggles, token),
        allowgc_enabled_view("Group <name>", GROUP, toggles, token),
        allowgc_enabled_view("Group <name>", GROUP, toggles, token, show_save=True),
        allowgc_turned_off_view("Group <name>", GROUP, token),
        allowgc_imout_confirm_view(GROUP, "Group <name>"),
    ):
        request = edit_request(types.InputPeerUser(100, 0), 55, text, reply_markup=markup)
        assert isinstance(request.rich_message, types.InputRichMessageHTML)
        assert bytes(request)
        assert "Group &lt;name&gt;" in request.rich_message.html
        _no_emoji(request.rich_message.html)
    save_text, _save_markup = allowgc_enabled_view(
        "Group <name>", GROUP, toggles, token, show_save=True,
    )
    assert "<p>Status:" in save_text
    assert '<tg-button type="disabled" style="success">enabled</tg-button>' in save_text
    assert '<tg-button type="disabled" style="danger">off</tg-button>' in save_text
    assert "Click the buttons to turn access on or off." in save_text
    assert "This confirmation expired." in allowgc_expired_view()
    turned_off, turned_off_markup = allowgc_turned_off_view("Group <name>", GROUP, token)
    assert "preserved for 7 days" in turned_off
    assert "send /allowgc again within 7 days" in turned_off
    # Same Allowgc + beta heading as every other state, a red badge on
    # "off", and the divided turn-on hint — never the old h2 heading.
    assert "<p><b>Allowgc</b>" in turned_off and 'data="badge:beta"' in turned_off
    assert '<tg-button type="disabled" style="danger">off</tg-button>' in turned_off
    assert "Click turn on to enable it again." in turned_off
    assert "<h2>Allowgc turned off</h2>" not in turned_off
    turnon = _payload(turned_off_markup, "hm:allowgc:turnon:")
    assert re.fullmatch(r"hm:allowgc:turnon:[0-9a-f]{32}", turnon)
    expired = allowgc_expired_view()
    assert "belongs to another chat" not in expired


def test_allowgc_enabled_status_is_a_word_badge_not_a_full_line_badge():
    from plugins.platforms.telegram.access_ui import allowgc_enabled_view

    toggles = {"tools": True, "web": False, "shell": True, "trigger": True}
    text, _markup = allowgc_enabled_view(
        "Group <name>", GROUP, toggles, "f" * 32, show_save=True,
    )
    # Status is the word "enabled" inside an inert badge (green), on a
    # "Status:" line — not one badge holding the whole sentence. Each
    # access bullet carries its own green/red on/off word badge.
    assert "<p>Status:" in text
    assert '<tg-button type="disabled" style="success">enabled</tg-button>' in text
    assert "Status: Enabled" not in text
    assert '<tg-button type="disabled" style="danger">off</tg-button>' in text
    assert '<tg-button type="disabled" style="success">on</tg-button>' in text
    assert text.count("<p>Status:") == 1
    _no_emoji(text)


def test_allowgc_toggle_rows_pair_into_two_rows():
    from plugins.platforms.telegram.access_ui import _allowgc_toggle_rows
    from plugins.platforms.telegram.mtproto_rich import inline_markup

    toggles = {"tools": True, "web": False, "shell": True, "trigger": True}
    markup = inline_markup(_allowgc_toggle_rows(toggles, "f" * 32))
    rows = [[button.text for button in row.buttons] for row in markup.rows]
    assert rows == [["Tools: On", "Web: Off"], ["Shell: On", "Trigger: On"]]


@pytest.mark.asyncio
async def test_allowgc_member_takes_normal_path_not_tenant(transport):
    from agent.mongo_tenants import Principal

    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    member = _source(MEMBER)

    await command(transport, "@NainaAssistantBot hello", member)

    group_store = store_for_home(gc_home)
    group_access = group_store.access_for_scope(MEMBER, "group", GROUP)
    assert group_access["allowed"] is True
    assert group_access["sudo"] is False
    assert group_store.get_access(MEMBER)["allowed"] is False
    assert transport.store.get_access(MEMBER)["allowed"] is False
    transport.tenant_handler.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()

    # Control: the same member in a plain group is not auto-admitted and
    # takes no normal turn; an admitted member there takes the tenant path.
    adapter.handle_message.reset_mock()
    transport.tenant_handler.reset_mock()
    await command(transport, "@NainaAssistantBot hello", _source(MEMBER, chat_id=OTHER_GROUP))
    assert transport.store.get_access(MEMBER)["allowed"] is False
    adapter.handle_message.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()
    transport.store.allow_user(Principal(OWNER), MEMBER, True)
    await command(transport, "@NainaAssistantBot hello", _source(MEMBER, chat_id=OTHER_GROUP))
    transport.tenant_handler.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowgc_member_trigger_is_silently_denied(transport):
    """Sudo needs a real profile: a member cannot run /trigger in the group."""
    from plugins.platforms.telegram.telegram_chat_settings import load_chat_settings

    adapter = transport.adapter
    await enable_allowgc(transport)
    gc_home = resolve_home(GROUP, reap=False)

    adapter.send.reset_mock()
    await command(transport, "/trigger heybot", _source(MEMBER))
    adapter.send.assert_not_awaited()
    assert load_chat_settings(gc_home, "group", GROUP).get("trigger_word") in (None, "")

    await command(transport, "/trigger heybot", _source(OWNER))
    assert load_chat_settings(gc_home, "group", GROUP).get("trigger_word") == "heybot"


@pytest.mark.asyncio
async def test_allowgc_trigger_mode_admits_member_word_invocation(transport):
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings

    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER)
    update_chat_settings(gc_home, "group", GROUP, {"trigger_word": "heybot", "trigger_mode": "trigger"})
    member = _source(MEMBER)

    assert await adapter._group_triggered(_message("heybot hello", member), member) is True
    assert await adapter._group_triggered(_message("hello", member), member) is False

    # Control: the same word in a non-allowgc group in trigger mode stays silent.
    update_chat_settings(transport.home, "group", OTHER_GROUP, {"trigger_word": "heybot", "trigger_mode": "trigger"})
    other = _source(MEMBER, chat_id=OTHER_GROUP)
    assert await adapter._group_triggered(_message("heybot hello", other), other) is False


@pytest.mark.asyncio
async def test_allowgc_trigger_off_blocks_trigger_word_only(transport):
    from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings

    adapter = transport.adapter
    gc_home = create_group_profile(GROUP, f"Chat {GROUP}", OWNER, toggles={"trigger": False})
    update_chat_settings(gc_home, "group", GROUP, {"trigger_word": "heybot"})
    source = _source(MEMBER)
    word_event = _message("heybot hello", source)
    mention_event = _message("@NainaAssistantBot hello", source)

    assert await adapter._group_triggered(word_event, source) is False
    assert await adapter._group_triggered(mention_event, source) is True

    from gateway.allowgc import set_toggle
    set_toggle(gc_home, "trigger", True)
    assert await adapter._group_triggered(word_event, source) is True


def test_allowgc_confirmation_card_has_no_what_this_does_section():
    """The /allowgc confirm card drops the "What this does" collapsible entirely."""
    from plugins.platforms.telegram.access_ui import allowgc_confirmation_view

    text, markup = allowgc_confirmation_view(
        "Group <name>", GROUP,
        {"tools": True, "web": False, "shell": True, "trigger": True}, "a" * 32)
    assert "What this does" not in text
    assert "<details>" not in text
    assert "/imout" not in text
    # The rest of the card is unchanged: heading, the allow-every-member
    # paragraph, the shared-profile paragraph, the italic footer, and the
    # Confirm/Cancel buttons.
    assert "Allow every member of" in text
    assert "shares one profile" in text
    assert "Click confirm to enable it in this group." in text
    assert _payload(markup, "hm:allowgc:confirm:").startswith("hm:allowgc:confirm:")
    assert _payload(markup, "hm:allowgc:cancel:").startswith("hm:allowgc:cancel:")


@pytest.mark.asyncio
async def test_non_admitted_member_command_drop_is_logged(transport, caplog):
    """A non-admitted member's command in a group with no allowgc profile
    is dropped silently — but the drop leaves a log line with actor + chat
    id, so the next invisible user is identifiable."""
    import logging

    adapter = transport.adapter
    with caplog.at_level(logging.INFO):
        await command(transport, "/definitely-not-a-command", _source(MEMBER, chat_id=OTHER_GROUP), message_id=80)

    assert "not admitted" in caplog.text
    assert MEMBER in caplog.text and OTHER_GROUP in caplog.text
    adapter.send.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()
