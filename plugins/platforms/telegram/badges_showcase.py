"""The /badges showcase: every Telegram rich button kind and style, in every placement.

A two-page rich card. Page 1 shows the five kinds (url, callback, copy,
profile, disabled) and the four styles (primary, success, danger, link) in
a paragraph, in button rows, in a kind-by-style table, and inside a
collapsible. Page 2 shows the two forms the user actually lives with — the
footer badges under a reply and the new/beta badges in settings — plus
disabled and link-style buttons. A pager flips the pages in place; every
showcase button answers a toast naming its kind, style, and placement.

Callback data (``bd:`` prefix, all well under Telegram's 64-byte limit):

- ``bd:p:<n>`` — pager, go to page *n* (edits the message in place).
- ``bd:i:<kind>:<style>:<place>`` — info toast for a showcase button.
- ``bd:close`` — delete the showcase message.

The ``bd:`` taps answer before the multiuser gate, like the footer badges:
a showcase button is a read-only peek at a message anyone in the chat can
already see, so it carries no owner, user, group or admission check — click
and you get the answer. The pager edits the bot's own message, nothing else.
"""

from __future__ import annotations

import logging
from typing import Any

from .callback_answer import answer_callback
from .mtproto_rich import rich_button, rich_button_row, rich_page_footer

logger = logging.getLogger(__name__)

_KINDS = ("callback", "url", "copy", "profile", "disabled")
_STYLES = ("primary", "success", "danger", "link")
_PLACES = {
    "para": "a paragraph",
    "row": "a button row",
    "table": "a table",
    "details": "a collapsible",
    "footer": "the footer",
    "settings": "settings",
}
_PAGES = 2


def _info_data(kind: str, style: str, place: str) -> str:
    """Callback data for a showcase button: kind, style, placement."""
    return f"bd:i:{kind}:{style}:{place}"


def _info_button(label: str, kind: str, style: str | None, place: str,
                 value: str | None = None) -> str:
    """A showcase button whose callback data names what it is. *value* is
    the button's own payload (a URL, text to copy, a user id); when it is
    None the info data itself is the payload for a callback button."""
    if kind == "disabled":
        # Link style works only for callback buttons; a disabled button
        # standing in the link column renders with the default style.
        return rich_button(label, kind="disabled",
                           style=None if style == "link" else style)
    if kind == "callback":
        return rich_button(label, kind="callback",
                           value=_info_data(kind, style or "primary", place),
                           style=style)
    return rich_button(label, kind=kind, value=value or "",
                       style=style)


def _profile_button(label: str, user_id: str, style: str | None,
                    place: str) -> str:
    """A profile button for *user_id*; a disabled button when the id is not
    a number, so the showcase never builds a broken button."""
    if str(user_id or "").strip().isdecimal():
        return rich_button(label, kind="profile", value=str(user_id).strip(),
                           style=style)
    return rich_button(label, kind="disabled", style=style)


def _pager_row(page: int) -> str:
    """Back / Next / Close for *page*. The live direction is a callback
    button; the dead end is a disabled button, never a missing one."""
    back = (rich_button("‹ Back", kind="callback", value="bd:p:1", style="primary")
            if page == 2 else
            rich_button("‹ Back", kind="disabled", style="primary"))
    nxt = (rich_button("Next ›", kind="callback", value="bd:p:2", style="primary")
           if page == 1 else
           rich_button("Next ›", kind="disabled", style="primary"))
    close = rich_button("Close", kind="callback", value="bd:close", style="danger")
    return rich_button_row([back, nxt, close], align="center")


def _page1_body(user_id: str) -> str:
    """Page 1: kinds and styles in a paragraph, rows, a table, a collapsible."""
    parts = ["<h1>Rich buttons</h1>",
             "<p>Every kind and style Telegram rich buttons can take, in "
             "every place they can sit. Tap any button — it answers what it is.</p>"]

    # Paragraph: all five kinds nested inline in running text.
    parts.append(
        "<p>Buttons can live inside a sentence: tap "
        + _info_button("this callback", "callback", "link", "para")
        + " for a toast, open "
        + _info_button("Telegram", "url", "primary", "para", "https://telegram.org")
        + ", copy "
        + _info_button("this text", "copy", "success", "para", "rich buttons")
        + ", visit "
        + _profile_button("a profile", user_id, "primary", "para")
        + ", or press "
        + _info_button("nothing", "disabled", "primary", "para")
        + " — it does nothing.</p>")

    # Button rows: one per alignment, each showing different styles.
    parts.append("<p><b>Rows</b> — left, center, right:</p>")
    parts.append(rich_button_row([
        _info_button("Primary", "callback", "primary", "row"),
        _info_button("Success", "callback", "success", "row"),
        _info_button("Danger", "callback", "danger", "row"),
    ], align="left"))
    parts.append(rich_button_row([
        _info_button("Telegram.org", "url", "primary", "row", "https://telegram.org"),
        _info_button("Copy me", "copy", "success", "row", "copy me"),
        _profile_button("My profile", user_id, "primary", "row"),
    ], align="center"))
    parts.append(rich_button_row([
        _info_button("Link style", "callback", "link", "row"),
        _info_button("Disabled", "disabled", "primary", "row"),
    ], align="right"))

    # Kind-by-style table: every kind against every style. Link style works
    # only for callback buttons, so the other rows show a disabled button
    # there — the form, with no working tap behind it.
    rows = ["<table><tr><th>Kind</th><th>primary</th><th>success</th>"
            "<th>danger</th><th>link</th></tr>"]
    for kind in _KINDS:
        cells = [f"<td><b>{kind}</b></td>"]
        for style in _STYLES:
            if style == "link" and kind != "callback":
                cells.append(f"<td>{_info_button('link', 'disabled', style, 'table')}</td>")
            elif kind == "url":
                cells.append(f"<td>{_info_button(style, 'url', style, 'table', 'https://telegram.org')}</td>")
            elif kind == "copy":
                cells.append(f"<td>{_info_button(style, 'copy', style, 'table', f'{kind} {style}')}</td>")
            elif kind == "profile":
                cells.append(f"<td>{_profile_button(style, user_id, style, 'table')}</td>")
            elif kind == "disabled":
                cells.append(f"<td>{_info_button(style, 'disabled', style, 'table')}</td>")
            else:
                cells.append(f"<td>{_info_button(style, 'callback', style, 'table')}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    rows.append("</table>")
    parts.append("<p><b>Kind × style</b> — link style works only for "
                 "callback buttons; the other rows show a disabled button there.</p>")
    parts.append("".join(rows))

    # Collapsible: buttons work inside <details> too.
    parts.append(
        "<details><summary>Inside a collapsible</summary><p>Buttons work in "
        "here too: "
        + _info_button("copy this", "copy", "success", "details", "inside a collapsible")
        + " "
        + _info_button("open docs", "url", "primary", "details", "https://telegram.org")
        + " "
        + _info_button("toast me", "callback", "link", "details")
        + "</p></details>")

    return "".join(parts)


def _page2_body(user_id: str) -> str:
    """Page 2: the two forms the user actually lives with, plus disabled
    and link-style buttons."""
    parts = ["<h1>Badges in the wild</h1>",
             "<p>The two forms you actually live with: the footer badges "
             "under a reply, and the new/beta badges in settings.</p>"]

    # Footer badges: the footer line as inline badges, like the real footer.
    parts.append("<p><b>Footer badges</b> — model, context %, cwd, each one "
                 "answering its detail:</p>")
    parts.append(
        "<p>"
        + _info_button("gpt-6-luna", "callback", "primary", "footer")
        + " · "
        + _info_button("14%", "callback", "primary", "footer")
        + " · "
        + _info_button("~", "callback", "primary", "footer")
        + "</p>")

    # Settings badges: the real badge: callbacks, so they answer the real
    # toasts — this is the actual settings-badge mechanism, not a copy.
    parts.append("<p><b>Settings badges</b> — the real ones, right after the "
                 "setting name:</p>")
    parts.append(
        "<table><tr><th>Setting</th><th>Value</th></tr>"
        "<tr><td><b>Streaming response</b> "
        '<tg-button type="callback_data" style="primary" data="badge:beta">beta</tg-button>'
        "</td><td>Off</td></tr>"
        "<tr><td><b>Autonomous mode</b> "
        '<tg-button type="callback_data" style="primary" data="badge:new">new</tg-button>'
        "</td><td>Off</td></tr></table>")

    # Disabled buttons in every style.
    parts.append("<p><b>Disabled</b> — every style, none of them doing anything:</p>")
    parts.append(rich_button_row([
        _info_button("primary", "disabled", "primary", "row"),
        _info_button("success", "disabled", "success", "row"),
        _info_button("danger", "disabled", "danger", "row"),
        _info_button("link", "disabled", "link", "row"),
    ], align="left"))

    # Link-style buttons: the small inline form.
    parts.append("<p><b>Link style</b> — small, inline, callback only: "
                 + _info_button("one", "callback", "link", "para")
                 + " "
                 + _info_button("two", "callback", "link", "para")
                 + " "
                 + _info_button("three", "callback", "link", "para")
                 + "</p>")
    return "".join(parts)


def render_badges_page(page: int, user_id: str = "") -> str:
    """The full showcase HTML for *page* (1 or 2). The page index names the
    total button count across both pages, so the footer reads like the
    settings pages do."""
    page = 2 if int(page or 1) == 2 else 1
    body1 = _page1_body(user_id)
    body2 = _page2_body(user_id)
    pager1 = _pager_row(1)
    pager2 = _pager_row(2)
    # Count only button elements (``<tg-button `` with a space), never the
    # row tags, and include the pager buttons in the total.
    total = ((body1 + pager1).count("<tg-button ")
             + (body2 + pager2).count("<tg-button "))
    body = body1 if page == 1 else body2
    pager = pager1 if page == 1 else pager2
    return body + rich_page_footer(page, _PAGES, total) + pager


def _info_toast(kind: str, style: str, place: str) -> str | None:
    """The toast for a ``bd:i:`` tap: kind, style, placement. Unknown parts
    answer empty and never invent a toast."""
    if kind not in _KINDS or style not in (*_STYLES, "none") or place not in _PLACES:
        return None
    style_text = "default style" if style == "none" else f"{style} style"
    return f"{kind} button · {style_text} · in {_PLACES[place]}"


async def handle_badges_callback(adapter: Any, event: Any, data: str) -> bool:
    """Handle one ``bd:`` tap: pager, info toast, or close.

    The pager edits the bot's own showcase message in place; an info tap
    answers its toast; Close deletes the message. Every path answers the
    query (empty when the tap already did its work), so the button never
    spins. A tap on a message that is gone answers that plainly.
    """
    raw = str(data or "")
    if raw == "bd:close":
        try:
            source = await adapter._source(event)
            await adapter.delete_message(str(source.chat_id), str(event.message_id))
        except Exception:  # health: allow BLE001 -- a failed delete must not break the tap answer; the message is already gone or unreachable
            logger.debug("Could not delete the /badges showcase message", exc_info=True)
        return await answer_callback(event, None)
    if raw.startswith("bd:p:"):
        try:
            page = int(raw.split(":")[2])
        except (IndexError, ValueError):
            return await answer_callback(event, None)
        if page not in (1, 2):
            return await answer_callback(event, None)
        user_id = str(getattr(event, "sender_id", "") or "")
        html = render_badges_page(page, user_id=user_id)
        try:
            source = await adapter._source(event)
            result = await adapter.edit_message(
                str(source.chat_id), str(event.message_id), html)
        except Exception:  # health: allow BLE001 -- a failed edit answers its toast, not an error; the message may be gone
            return await answer_callback(event, "Could not turn the page.")
        if result is not None and not getattr(result, "success", False):
            return await answer_callback(event, "Could not turn the page.")
        return await answer_callback(event, None)
    if raw.startswith("bd:i:"):
        parts = raw.split(":")
        toast = _info_toast(
            parts[2] if len(parts) > 2 else "",
            parts[3] if len(parts) > 3 else "",
            parts[4] if len(parts) > 4 else "")
        return await answer_callback(event, toast)
    return await answer_callback(event, None)
