"""Exec-approval card states for the Telegram adapter.

The guard's approval prompt is one card. While pending, the header reads
"Hermes needs your ok •" with ONLY the status words inside a small red
badge ("approval pending") — the parentheses in the prompt text are the
badge boundaries, never part of the badge label and never plain text.
The reason hides in a ``<details>`` collapsible and the 5-minute deadline
rides as a highlighted blockquote. The actions are a regular inline
keyboard ("Allow Once" / "Deny", same ``ea:<choice>:<id>`` data as the
previous build's in-body badges). The pending header badge, the granted
badge and the timed-out badge answer a short toast on tap (``badge:pending`` /
``badge:granted`` / ``badge:timeout``); the denied badge stays a disabled
indicator, and no badge ever acts. Once decided, the card is edited in place — no new
"approved, now proceeding" message — and the decided state rides at the
TOP: bold "Command ran" plus a small green badge holding only "approval
granted" (or bold "Command denied" plus a red "approval denied" badge),
with "Approved/Denied by <person>" below. A timed-out card rides the same
shape — bold "Command didn't run" plus a small red badge holding only
"timed out", the command, and the timeout notice as the footer — with no
"Why it was flagged" and no auto-delete. The settled edit carries no
keyboard — a text-only edit clears the pending one — and an approved
card is deleted a minute later so the group sees the decision without
keeping the clutter.
"""

from __future__ import annotations

import logging
import re
from html import escape
from typing import Any

logger = logging.getLogger(__name__)

#: How long an approved card stays visible after the edit, so the group sees
#: who approved what, before it is deleted (same transient treatment as
#: working messages).
APPROVED_CARD_DELETE_AFTER_S = 60.0

#: Settled status ``(bold lead, badge label, badge style, toast callback)``
#: per decision. The badge holds ONLY the status words — the parentheses in
#: the spec ("Command ran • (approval granted)") are its boundaries. The
#: granted badge answers its toast on tap; the denied badge stays a
#: disabled indicator (it is not in the badge list).
_SETTLED_STATUS = {
    "approved": ("Command ran", "approval granted", "success", "badge:granted"),
    "denied": ("Command denied", "approval denied", "danger", None),
}

#: Toast callback per pending header badge label. Only labels in this map
#: get callback data; any other trailing ``(words)`` stays a disabled
#: indicator — no toast is ever invented for an unknown label.
_BADGE_CALLBACKS = {"approval pending": "badge:pending"}

_TRAILING_BADGE_RE = re.compile(r"^(?P<prefix>.*?)\s*\((?P<label>[^()]*)\)\s*$")


def _split_trailing_badge(text: str) -> tuple[str, str | None]:
    """Split a trailing ``(words)`` off *text*: parentheses are badge boundaries.

    Returns ``(plain_prefix, badge_label)``; ``badge_label`` is None when the
    text carries no trailing parenthetical. The label holds ONLY the words
    inside the brackets.
    """
    match = _TRAILING_BADGE_RE.match(str(text or "").strip())
    if match is None:
        return str(text or "").strip(), None
    label = match.group("label").strip()
    if not label:
        return str(text or "").strip(), None
    return match.group("prefix").strip(), label


def _status_badge(label: str, style: str, value: str | None = None) -> str:
    """One small status badge: a toast-only callback button when given
    callback data, else a disabled indicator."""
    from .mtproto_rich import rich_button

    if value is None:
        return rich_button(label, kind="disabled", style=style)
    return rich_button(label, kind="callback", value=value, style=style)


def _header_paragraph(header: str) -> str:
    """Header line: plain text, with a trailing ``(words)`` rendered as a
    small red status badge holding only those words."""
    from .command_cards import _inline_markup

    prefix, label = _split_trailing_badge(header)
    if label is None:
        return f"<p>{_inline_markup(prefix)}</p>"
    return (
        f"<p>{_inline_markup(prefix)} "
        f"{_status_badge(label, 'danger', value=_BADGE_CALLBACKS.get(label))}</p>"
    )


def pending_approval_keyboard(actions, approval_id):
    """Regular inline keyboard for the pending approval actions.

    Actions ride as normal Telegram inline keyboard buttons (two per row)
    with the same ``ea:<choice>:<id>`` data the previous build's in-body
    badges carried. None when there is nothing to approve (or no id to bind
    the tap to).
    """
    from .mtproto_rich import inline_markup

    if approval_id is None:
        return None
    normalized = []
    for label, choice, style in actions or ():
        normalized.append((
            str(label),
            str(choice),
            str(style or "").strip().lower() or None,
        ))
    if not normalized:
        return None
    rows = [
        [(label, f"ea:{choice}:{approval_id}", style)
         for label, choice, style in normalized[index:index + 2]]
        for index in range(0, len(normalized), 2)
    ]
    return inline_markup(rows)


def _fallback_html(lines: list[str]) -> str:
    """Whole-body render for a prompt whose shape is not recognised: the
    first line still carries its trailing ``(words)`` as a red status badge;
    the rest renders as-is."""
    from .command_cards import lines_to_rich_html

    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return lines_to_rich_html("", lines)
    prefix, label = _split_trailing_badge(lines[first])
    if label is None:
        return lines_to_rich_html("", lines)
    head = _header_paragraph(lines[first])
    body = lines_to_rich_html("", lines[first + 1:])
    return head + ("\n" + body if body else "")


def pending_approval_html(prompt_text: str) -> str:
    """Pending card: header with a red status badge, then the prompt as rich HTML.

    The shared renderer builds the prompt as Markdown (header, fenced
    command, reason, deadline); the card restructures it instead of
    passing it through verbatim: the header stays a plain paragraph with
    its trailing ``(words)`` as a small red badge, the command survives
    as a ``<pre>`` block, the reason hides in a ``<details>`` collapsible,
    and the deadline rides as a highlighted blockquote. A prompt whose
    shape is not recognised falls back to the whole-body render with the
    header badge kept, so it still renders and still approves (the actions
    ride on the keyboard, not in the body).
    """
    from .command_cards import _inline_markup, lines_to_rich_html

    text = str(prompt_text or "")
    lines = text.split("\n")
    fence_open = next(
        (i for i, line in enumerate(lines) if line.strip().startswith("```")), None)
    fence_close = None
    if fence_open is not None:
        fence_close = next(
            (i for i in range(fence_open + 1, len(lines))
             if lines[i].strip().startswith("```")), None)
    if fence_open is None or fence_close is None:
        return _fallback_html(lines)

    from gateway.platforms.base_exec_approval import (
        approval_timeout_seconds, ea_reason_label_text,
        format_approval_deadline_line)

    header = " ".join(part.strip() for part in lines[:fence_open] if part.strip())
    command = "\n".join(lines[fence_open + 1:fence_close])
    rest = lines[fence_close + 1:]
    deadline = format_approval_deadline_line(approval_timeout_seconds())
    deadline_idx = next(
        (i for i, line in enumerate(rest) if line.strip() == deadline), None)
    if deadline_idx is None:
        non_empty = [i for i, line in enumerate(rest) if line.strip()]
        deadline_idx = non_empty[-1] if non_empty else None
    if deadline_idx is None:
        return _fallback_html(lines)
    deadline_line = rest[deadline_idx].strip()
    reason = "\n".join(rest[:deadline_idx]).strip()
    reason_label = ea_reason_label_text()
    if reason.startswith(f"{reason_label}:"):
        reason = reason[len(reason_label) + 1:].strip()
    trailing = [line for line in rest[deadline_idx + 1:] if line.strip()]

    parts = []
    if header:
        parts.append(_header_paragraph(header))
    parts.append("<pre>" + "\n".join(
        escape(line, quote=False) for line in command.split("\n")) + "</pre>")
    if reason:
        reason_html = "<br>".join(
            _inline_markup(line) for line in reason.split("\n") if line.strip())
        parts.append(
            f"<details><summary>{escape(reason_label)}</summary>"
            f"<p>{reason_html}</p></details>")
    parts.append(f"<blockquote>{_inline_markup(deadline_line)}</blockquote>")
    for line in trailing:
        parts.append(f"<p>{_inline_markup(line.strip())}</p>")
    return "\n".join(parts)


def settled_approval_html(
    *, approved: bool, command: str = "", approver_name: str = "", approver_id: str = "",
) -> str:
    """Decided card body: the status line on top, then the command and
    ``Approved/Denied by <person>`` below.

    The status line is bold normal text ("Command ran" / "Command denied")
    plus a small badge holding ONLY the status words ("approval granted" /
    "approval denied"). The granted badge answers its toast on tap
    (``badge:granted``); the denied badge stays a disabled indicator.
    No keyboard is attached. A denied card never looks approved: "Denied
    by" and no green anywhere.
    """
    from .mtproto_rich import rich_button

    state = "approved" if approved else "denied"
    verb = "Approved by" if approved else "Denied by"
    lead, label, style, callback = _SETTLED_STATUS[state]
    parts = [f"<p><b>{lead}</b> • {_status_badge(label, style, value=callback)}</p>"]
    if str(command or "").strip():
        parts.append(f"<pre>{escape(str(command))}</pre>")
    name = " ".join(str(approver_name or "").split()) or str(approver_id or "")
    if str(approver_id or "").strip().isdecimal():
        person = rich_button(name or str(approver_id), kind="profile", value=str(approver_id).strip())
    elif name:
        person = escape(name)
    else:
        person = "the owner"
    parts.append(f"<p>{verb} {person}</p>")
    return "\n".join(parts)


def timed_out_approval_html(*, command: str = "", notice: str = "") -> str:
    """Timed-out card body: the status line on top, then the command and
    the timeout notice as the footer.

    The status line is bold normal text ("Command didn't run") plus a small
    red badge holding ONLY the status words ("timed out"). The badge answers
    its toast on tap (``badge:timeout``). The reason is dropped entirely —
    the card keeps only the command and the footer notice. No keyboard is
    attached and the card is never auto-deleted: the command didn't run.
    """
    parts = [
        f"<p><b>Command didn't run</b> • "
        f"{_status_badge('timed out', 'danger', value='badge:timeout')}</p>"
    ]
    if str(command or "").strip():
        parts.append(f"<pre>{escape(str(command))}</pre>")
    if str(notice or "").strip():
        parts.append(f"<p>{escape(str(notice))}</p>")
    return "\n".join(parts)


async def settle_approval_card(
    adapter, *, chat_id: str, message_id, command: str, choice: str,
    approver_name: str = "", approver_id: str = "",
) -> bool:
    """Edit a decided approval card in place (see ``settled_approval_html``);
    the status line sits on top with the badge holding only the status
    words, and no keyboard is attached — a text-only edit clears the
    pending keyboard — so no action button survives the edit. An approved
    card is deleted a minute later so the group sees the decision without
    keeping the clutter. False when the edit itself failed."""
    from .message_self_delete import schedule_sent_message_self_delete
    from .mtproto_rich import edit_request

    approved = str(choice) != "deny"
    html = settled_approval_html(
        approved=approved, command=str(command or ""),
        approver_name=str(approver_name or ""), approver_id=str(approver_id or ""))
    try:
        await adapter._mtproto_rich_request(edit_request(
            adapter._peer_for(str(chat_id)), int(message_id), html,
        ))
    except Exception:
        logger.debug("Could not update the handled Telegram approval card", exc_info=True)
        return False
    if approved:
        schedule_sent_message_self_delete(
            adapter, {"telegram_self_delete_after_s": APPROVED_CARD_DELETE_AFTER_S},
            str(chat_id), message_id)
    return True


async def settle_timeout_approval_card(
    adapter, *, chat_id: str, message_id, command: str, timeout_s: int | None = None,
) -> bool:
    """Edit a timed-out approval card in place (see ``timed_out_approval_html``);
    the status line sits on top with the badge holding only the status
    words, and no keyboard is attached — a text-only edit clears the
    pending keyboard — so no action button survives the edit. Unlike an
    approved card, a timed-out one is never auto-deleted: the command
    didn't run, and the card stays as its record. False when the edit
    itself failed."""
    from gateway.platforms.base_exec_approval import (
        approval_timeout_seconds, format_approval_timed_out_notice)
    from .mtproto_rich import edit_request

    window_s = timeout_s if timeout_s is not None else approval_timeout_seconds()
    html = timed_out_approval_html(
        command=str(command or ""),
        notice=format_approval_timed_out_notice(window_s))
    try:
        await adapter._mtproto_rich_request(edit_request(
            adapter._peer_for(str(chat_id)), int(message_id), html,
        ))
    except Exception:
        logger.debug("Could not update the timed-out Telegram approval card", exc_info=True)
        return False
    return True


async def settle_exec_approval_cards(
    adapter, session_key: str, request_ids, choice: str, *,
    approver_name: str = "", approver_id: str = "", timeout_s: int | None = None,
) -> int:
    """Edit pending approval cards in place for approvals answered by typing
    instead of tapping (``/approve`` / ``/deny``). Matches the pending
    entries by request id within the session; cards the adapter never sent
    (or already settled) settle nothing, and the caller then falls back to
    its text confirmation. A ``timeout`` choice renders the timed-out card
    instead of the decided one. Returns how many cards were edited."""
    if adapter._client is None:
        return 0
    wanted = {str(rid) for rid in (request_ids or [])}
    if not wanted:
        return 0
    settled = 0
    for approval_id, state in list(adapter._approval_state.items()):
        if str(state.get("session_key") or "") != str(session_key):
            continue
        if str(state.get("request_id") or "") not in wanted:
            continue
        message_id = state.get("message_id")
        adapter._approval_state.pop(approval_id, None)
        if not message_id:
            continue
        if str(choice) == "timeout":
            settled_ok = await settle_timeout_approval_card(
                adapter, chat_id=str(state.get("chat_id") or ""), message_id=message_id,
                command=str(state.get("command") or ""), timeout_s=timeout_s,
            )
        else:
            settled_ok = await settle_approval_card(
                adapter, chat_id=str(state.get("chat_id") or ""), message_id=message_id,
                command=str(state.get("command") or ""), choice=choice,
                approver_name=str(approver_name or ""), approver_id=str(approver_id or ""),
            )
        if settled_ok:
            settled += 1
    return settled
