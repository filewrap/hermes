"""The /badges showcase: every rich button kind and style, in every placement.

Page 1 shows the five kinds and four styles in a paragraph, in button
rows, in a kind-by-style table, and inside a collapsible. Page 2 shows the
footer badges and the settings badges, plus disabled and link-style buttons.
A pager flips the pages in place; every showcase button answers a toast
naming its kind, style, and placement.
"""

import re
from types import SimpleNamespace

import pytest

from plugins.platforms.telegram.badges_showcase import (
    _info_toast,
    handle_badges_callback,
    render_badges_page,
)

_USER_ID = "6802929470"


class _Event:
    """Minimal Telethon callback-query event: records every answer."""

    def __init__(self, data):
        self.data = data
        self.message_id = 101
        self.sender_id = 6802929470
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class _Adapter:
    """Minimal adapter: records edits and deletes, answers a source."""

    def __init__(self):
        self.edits = []
        self.deletes = []

    async def _source(self, event):
        return SimpleNamespace(chat_id="123")

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.edits.append((chat_id, message_id, content))
        return SimpleNamespace(success=True, message_id=str(message_id))

    async def delete_message(self, chat_id, message_id):
        self.deletes.append((chat_id, message_id))
        return True


def _buttons(html):
    return re.findall(r"<tg-button [^>]*>", html)


def test_page1_shows_all_kinds_in_a_paragraph():
    html = render_badges_page(1, user_id=_USER_ID)
    assert "<h1>Rich buttons</h1>" in html
    # The paragraph holds one of each kind, nested inline in the sentence.
    assert 'type="callback_data" style="link" data="bd:i:callback:link:para"' in html
    assert 'type="url" style="primary" url="https://telegram.org"' in html
    assert 'type="copy_text" style="success" text="rich buttons"' in html
    assert f'url="tg://user?id={_USER_ID}"' in html
    assert 'type="disabled" style="primary">nothing</tg-button>' in html


def test_page1_shows_left_center_right_rows():
    html = render_badges_page(1, user_id=_USER_ID)
    assert '<tg-button-row align="left">' in html
    assert '<tg-button-row align="center">' in html
    assert '<tg-button-row align="right">' in html


def test_page1_table_covers_every_kind_against_every_style():
    html = render_badges_page(1, user_id=_USER_ID)
    assert "<table>" in html
    for kind in ("callback", "url", "copy", "profile", "disabled"):
        assert f"<td><b>{kind}</b></td>" in html
    # The callback row has a working link-style button; the other rows show
    # a disabled button in the link column, because link style works only
    # for callback buttons.
    assert 'data="bd:i:callback:link:table"' in html
    assert html.count('type="disabled">link</tg-button>') == 4


def test_page1_has_a_collapsible_with_buttons_inside():
    html = render_badges_page(1, user_id=_USER_ID)
    assert "<details><summary>Inside a collapsible</summary>" in html
    assert 'data="bd:i:callback:link:details"' in html


def test_page2_shows_footer_badges_inline():
    html = render_badges_page(2, user_id=_USER_ID)
    assert "<h1>Badges in the wild</h1>" in html
    # The footer line as inline badges, joined like the real footer.
    assert 'data="bd:i:callback:primary:footer">gpt-6-luna</tg-button>' in html
    assert 'data="bd:i:callback:primary:footer">14%</tg-button>' in html
    assert 'data="bd:i:callback:primary:footer">~</tg-button>' in html


def test_page2_shows_the_real_settings_badges():
    html = render_badges_page(2, user_id=_USER_ID)
    # The real badge: callbacks, so they answer the real toasts.
    assert 'data="badge:beta">beta</tg-button>' in html
    assert 'data="badge:new">new</tg-button>' in html
    assert "<b>Streaming response</b>" in html


def test_page_index_names_the_same_total_on_both_pages():
    page1 = render_badges_page(1, user_id=_USER_ID)
    page2 = render_badges_page(2, user_id=_USER_ID)
    total = len(_buttons(page1)) + len(_buttons(page2))
    assert f"Page 1 of 2 • total {total}" in page1
    assert f"Page 2 of 2 • total {total}" in page2


def test_every_callback_data_fits_telegram_64_byte_limit():
    for page in (1, 2):
        html = render_badges_page(page, user_id=_USER_ID)
        for data in re.findall(r'data="([^"]+)"', html):
            assert len(data.encode("utf-8")) <= 64, data


def test_pager_back_is_dead_on_page1_and_next_is_dead_on_page2():
    page1 = render_badges_page(1, user_id=_USER_ID)
    page2 = render_badges_page(2, user_id=_USER_ID)
    assert 'type="disabled" style="primary">‹ Back</tg-button>' in page1
    assert 'data="bd:p:2">Next ›</tg-button>' in page1
    assert 'data="bd:p:1">‹ Back</tg-button>' in page2
    assert 'type="disabled" style="primary">Next ›</tg-button>' in page2
    assert 'data="bd:close">Close</tg-button>' in page1
    assert 'data="bd:close">Close</tg-button>' in page2


def test_profile_buttons_fall_back_to_disabled_without_a_numeric_id():
    html = render_badges_page(1, user_id="")
    assert "tg://user?id=" not in html
    assert 'type="disabled" style="primary">a profile</tg-button>' in html


def test_info_toast_names_kind_style_and_placement():
    assert _info_toast("callback", "link", "para") == (
        "callback button · link style · in a paragraph")
    assert _info_toast("url", "primary", "row") == (
        "url button · primary style · in a button row")
    assert _info_toast("copy", "success", "table") == (
        "copy button · success style · in a table")
    assert _info_toast("profile", "danger", "details") == (
        "profile button · danger style · in a collapsible")


def test_info_toast_answers_empty_for_unknown_parts():
    assert _info_toast("mystery", "primary", "para") is None
    assert _info_toast("callback", "sparkle", "para") is None
    assert _info_toast("callback", "primary", "nowhere") is None


@pytest.mark.asyncio
async def test_info_tap_answers_its_toast():
    event = _Event("bd:i:callback:link:para")
    assert await handle_badges_callback(_Adapter(), event, "bd:i:callback:link:para") is True
    assert event.answers == [(("callback button · link style · in a paragraph",), {})]


@pytest.mark.asyncio
async def test_pager_edits_the_message_in_place_with_page2():
    adapter = _Adapter()
    event = _Event("bd:p:2")
    assert await handle_badges_callback(adapter, event, "bd:p:2") is True
    assert event.answers == [((), {})]
    assert len(adapter.edits) == 1
    chat_id, message_id, html = adapter.edits[0]
    assert chat_id == "123" and message_id == "101"
    assert "Page 2 of 2" in html
    assert "Badges in the wild" in html


@pytest.mark.asyncio
async def test_pager_back_to_page1_and_bad_page_answer_empty():
    adapter = _Adapter()
    event = _Event("bd:p:1")
    assert await handle_badges_callback(adapter, event, "bd:p:1") is True
    assert "Page 1 of 2" in adapter.edits[0][2]
    event = _Event("bd:p:9")
    assert await handle_badges_callback(adapter, event, "bd:p:9") is True
    assert event.answers == [((), {})]
    assert len(adapter.edits) == 1


@pytest.mark.asyncio
async def test_close_deletes_the_showcase_message():
    adapter = _Adapter()
    event = _Event("bd:close")
    assert await handle_badges_callback(adapter, event, "bd:close") is True
    assert adapter.deletes == [("123", "101")]
    assert event.answers == [((), {})]


@pytest.mark.asyncio
async def test_unknown_bd_data_answers_empty():
    event = _Event("bd:nope")
    assert await handle_badges_callback(_Adapter(), event, "bd:nope") is True
    assert event.answers == [((), {})]


def test_badges_command_is_registered_and_plain():
    from gateway.run_busy import GatewayBusySessionMixin
    from hermes_cli.commands import COMMAND_REGISTRY

    names = {c.name for c in COMMAND_REGISTRY}
    assert "badges" in names
    assert "badges" in GatewayBusySessionMixin._PLAIN_COMMANDS
