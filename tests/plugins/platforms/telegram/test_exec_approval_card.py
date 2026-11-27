"""Regression tests for the exec-approval card states (badges hold ONLY the status words).

Approving edits the original card in place — never a new "approved, now
proceeding" message. While pending, the header reads "Hermes needs your
ok •" with ONLY the status words inside a small red badge ("approval
pending") — the parentheses in the prompt text are the badge boundaries —
the reason hides in a ``<details>`` collapsible, and the 5-minute
deadline rides as a highlighted blockquote. The actions are a regular
inline keyboard ("Allow Once" / "Deny", same ``ea:<choice>:<id>`` data),
never in-body badges. Once decided, the status line moves to the TOP:
bold "Command ran" plus a small green badge holding only "approval
granted" (or bold "Command denied" plus a red "approval denied" badge),
with "Approved/Denied by <person>" below and no keyboard attached at all.
An approved card is deleted a minute later.
"""

import itertools
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.platforms.base import Platform
from gateway.platforms.base_exec_approval import render_exec_approval
from plugins.platforms.telegram import exec_approval_card as card
from plugins.platforms.telegram import telethon_adapter as adapter_module
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

COMMAND = "rm -rf /tmp/stale-cache && systemctl restart hermes"
DESCRIPTION = "dangerous command: removes files outside the project"
CHAT_ID = "-1001234567890"
OWNER_ID = "6802929470"


class FakeClient:
    def __init__(self):
        self.sent_messages = []

    async def __call__(self, request):
        self.sent_messages.append(request)
        return SimpleNamespace(id=101)


def _pending_text(adapter):
    text, _truncated = render_exec_approval(adapter, COMMAND, DESCRIPTION, False, True)
    return text


@pytest.fixture
def adapter():
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    adapter._client = FakeClient()
    adapter.config = SimpleNamespace(extra={})
    adapter.platform = Platform.TELEGRAM
    adapter.gateway_runner = Mock()
    adapter._approval_state = {}
    adapter._approval_counter = itertools.count(1)
    adapter._peer_for = lambda _chat_id: f"peer:{_chat_id}"
    adapter._authorized = lambda _src: True
    adapter._settings_home = lambda _src: "/tmp/test-home"
    adapter._sent_message_id = lambda res, **kw: str(res.id)
    return adapter


def _pending_prompt(adapter):
    return SimpleNamespace(
        text=_pending_text(adapter),
        actions=[("Allow Once", "once", "primary"), ("Allow Session", "session", None),
                 ("Always Allow", "always", None), ("Deny", "deny", "danger")],
        session_key="agent:main:telegram:group:-1001234567890",
        chat_id=CHAT_ID,
        metadata={"owner_user_id": OWNER_ID, "profile_home": "/tmp/test-home",
                  "user_id": OWNER_ID},
        request_id="req-1",
        command=COMMAND,
    )


def _approval_state(message_id="7"):
    return {
        "session_key": "agent:main:telegram:group:-1001234567890",
        "request_id": "req-1",
        "chat_id": CHAT_ID,
        "thread_id": None,
        "profile_home": "/tmp/test-home",
        "owner_user_id": OWNER_ID,
        "intended_user_id": OWNER_ID,
        "owner_session": True,
        "allowed_choices": {"once", "session", "always", "deny"},
        "created_at": time.monotonic(),
        "message_id": message_id,
        "command": COMMAND,
    }


def _tap_event(message_id=7):
    sender = SimpleNamespace(id=int(OWNER_ID), first_name="Kailash", last_name=None,
                             username="kailashhh", bot=False)
    chat = SimpleNamespace(broadcast=False, megagroup=True, gigagroup=False, forum=False,
                           first_name=None, title="Test Group")
    message = SimpleNamespace(id=message_id, reply_to=None, action=None)
    return SimpleNamespace(message_id=message_id, chat_id=CHAT_ID, sender=sender,
                           chat=chat, message=message)


def _keyboard_buttons(markup):
    """``[(text, data, style)]`` for every button of a reply markup, row by row."""
    return [(button.text, button.type.data, button.style)
            for row in markup.rows for button in row.buttons]


# ── Pure card builders ────────────────────────────────────────────────

def test_pending_card_has_red_badge_reason_details_and_blockquote_deadline(adapter):
    """The pending card shows a small red badge holding ONLY "approval
    pending" in the header line, the command, the reason inside a
    <details> collapsible, and the deadline as a blockquote. No "Awaiting
    approval" pill anywhere, and "(approval pending)" never appears as
    plain text."""
    html = card.pending_approval_html(_pending_text(adapter))
    assert "<tg-button-row>" not in html
    assert "Awaiting approval" not in html
    assert "(approval pending)" not in html
    header = html.split("\n")[0]
    assert "Hermes needs your ok •" in header
    assert ">approval pending<" in header
    assert 'data="badge:pending"' in header
    assert 'type="disabled"' not in header
    assert 'style="danger"' in header
    assert 'style="success"' not in html
    assert "Hermes wants to run a command that needs your OK" not in html
    assert "<pre>rm -rf /tmp/stale-cache &amp;&amp; systemctl restart hermes</pre>" in html
    assert "<details><summary>Why it was flagged</summary>" in html
    assert "dangerous command: removes files outside the project" in html
    assert "Why it was flagged: dangerous command" not in html
    assert "<blockquote>If you don't answer within 5 minutes it will NOT run.</blockquote>" in html


def test_pending_card_keyboard_carries_actions_as_regular_buttons():
    """The actions ride as a regular inline keyboard (two per row) with the
    same ea: data the in-body badges carried — and the card body itself
    carries no callback data at all."""
    markup = card.pending_approval_keyboard(
        [("Allow Once", "once", "primary"), ("Deny", "deny", "danger")], 9)
    assert markup is not None
    assert [len(row.buttons) for row in markup.rows] == [2]
    buttons = _keyboard_buttons(markup)
    assert [text for text, _data, _style in buttons] == ["Allow Once", "Deny"]
    assert [data for _text, data, _style in buttons] == [b"ea:once:9", b"ea:deny:9"]
    assert buttons[0][2].bg_primary is True
    assert buttons[1][2].bg_danger is True
    assert markup.rows[0].buttons[0].text == "Allow Once"


def test_pending_card_keyboard_groups_four_actions_two_per_row():
    """Four actions group two per row, in order, with the session/always
    tiers unstyled."""
    markup = card.pending_approval_keyboard(
        [("Allow Once", "once", "primary"), ("Allow Session", "session", None),
         ("Always Allow", "always", None), ("Deny", "deny", "danger")], 9)
    assert [len(row.buttons) for row in markup.rows] == [2, 2]
    buttons = _keyboard_buttons(markup)
    assert [text for text, _data, _style in buttons] == [
        "Allow Once", "Allow Session", "Always Allow", "Deny"]
    assert [data for _text, data, _style in buttons] == [
        b"ea:once:9", b"ea:session:9", b"ea:always:9", b"ea:deny:9"]
    assert buttons[1][2].bg_primary is None and buttons[1][2].bg_danger is None


def test_pending_card_without_actions_has_no_keyboard(adapter):
    """No actions and no id: no keyboard, no ea: data anywhere — but
    the header badge still renders with its badge: toast data."""
    assert card.pending_approval_keyboard([], 9) is None
    assert card.pending_approval_keyboard([("Allow Once", "once", "primary")], None) is None
    html = card.pending_approval_html(_pending_text(adapter))
    assert "ea:" not in html
    assert ">approval pending<" in html
    assert 'data="badge:pending"' in html


def test_pending_card_fallback_keeps_header_badge():
    """A prompt whose shape is not recognised still renders the header
    with its red status badge and no "Awaiting approval" pill."""
    html = card.pending_approval_html(
        "Hermes needs your ok • (approval pending)\njust some text")
    assert "Awaiting approval" not in html
    assert "(approval pending)" not in html
    assert ">approval pending<" in html
    assert 'data="badge:pending"' in html
    assert 'style="danger"' in html
    assert "just some text" in html


def test_settled_approved_card_has_status_on_top():
    """Approved: the status line sits on top — bold "Command ran" plus a
    small green badge holding ONLY "approval granted" — then the command
    and "Approved by <person>" below. badge:granted callback data, no
    keyboard row, no heading."""
    html = card.settled_approval_html(
        approved=True, command=COMMAND, approver_name="Kailash", approver_id=OWNER_ID)
    assert "<h1>" not in html
    assert "<tg-button-row>" not in html
    assert "ea:" not in html
    status = html.split("\n")[0]
    assert "<b>Command ran</b>" in status
    assert ">approval granted<" in status
    assert 'data="badge:granted"' in status
    assert 'type="disabled"' not in status
    assert 'style="success"' in status
    assert html.index("<b>Command ran</b>") < html.index("<pre>")
    assert html.index("<pre>") < html.index("Approved by")
    assert "<pre>rm -rf /tmp/stale-cache &amp;&amp; systemctl restart hermes</pre>" in html
    assert "Approved by" in html
    assert f'tg://user?id={OWNER_ID}' in html
    assert "Kailash" in html
    assert "Denied" not in html


def test_settled_denied_card_has_status_on_top():
    """Denied: bold "Command denied" plus a small red badge holding ONLY
    "approval denied" on top — no green anywhere, no 'approved' wording,
    no callback data."""
    html = card.settled_approval_html(
        approved=False, command=COMMAND, approver_name="Kailash", approver_id=OWNER_ID)
    assert "<h1>" not in html
    assert "<tg-button-row>" not in html
    assert "ea:" not in html
    status = html.split("\n")[0]
    assert "<b>Command denied</b>" in status
    assert ">approval denied<" in status
    assert 'type="disabled"' in status
    assert 'style="danger"' in status
    assert 'style="success"' not in html
    assert "Command approved" not in html
    assert "Command ran" not in html
    assert "Approved by" not in html
    assert "Denied by" in html


def test_settled_card_without_command_or_name():
    """No command: no <pre>. No name or id: the approver falls back to the owner."""
    html = card.settled_approval_html(approved=True)
    assert "<pre>" not in html
    assert "Approved by the owner" in html
    assert "<b>Command ran</b>" in html
    assert ">approval granted<" in html


# ── Timed-out card ────────────────────────────────────────────────────

def test_timed_out_card_has_status_on_top_command_and_footer_notice():
    """Timed out: bold "Command didn't run" plus a small red badge holding
    ONLY "timed out" on top, then the command and the timeout notice as the
    footer. No "Why it was flagged" anywhere — the reason is dropped
    entirely — and no keyboard row."""
    from gateway.platforms.base_exec_approval import format_approval_timed_out_notice

    html = card.timed_out_approval_html(
        command=COMMAND, notice=format_approval_timed_out_notice(300))
    assert "<h1>" not in html
    assert "<tg-button-row>" not in html
    assert "ea:" not in html
    status = html.split("\n")[0]
    assert "<b>Command didn't run</b>" in status
    assert ">timed out<" in status
    assert 'data="badge:timeout"' in status
    assert 'style="danger"' in status
    assert 'style="success"' not in html
    assert html.index("<b>Command didn't run</b>") < html.index("<pre>")
    assert "<pre>rm -rf /tmp/stale-cache &amp;&amp; systemctl restart hermes</pre>" in html
    assert "Why it was flagged" not in html
    assert "Approval timed out after 5 minutes" in html
    assert html.index("</pre>") < html.index("Approval timed out")


# ── Adapter: tap path ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tap_approve_settles_with_badge_line_and_no_keyboard(adapter, monkeypatch):
    """A tap settles the card with the status line on top ("Command ran" +
    green "approval granted" badge) and NO keyboard attached — and still
    schedules the 1-minute delete."""
    answers = []
    deletions = []

    async def _answer(event, text=None, show_alert=False):
        answers.append({"text": text, "alert": show_alert})

    def _schedule_delete(_adapter, metadata, chat_id, message_id):
        deletions.append({"delay": metadata.get("telegram_self_delete_after_s"),
                          "chat_id": chat_id, "message_id": message_id})

    monkeypatch.setattr(adapter_module, "answer_callback", _answer)
    monkeypatch.setattr(
        "plugins.platforms.telegram.message_self_delete.schedule_sent_message_self_delete",
        _schedule_delete)
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", lambda *a, **k: 1)
    adapter._approval_state[1] = _approval_state()

    await adapter._handle_exec_approval_callback(_tap_event(), "ea:once:1")

    assert 1 not in adapter._approval_state  # popped: a second tap cannot re-resolve
    assert len(adapter._client.sent_messages) == 1  # one edit, no new message
    request = adapter._client.sent_messages[0]
    assert request.id == 7
    assert "<h1>" not in request.message
    assert "<tg-button-row>" not in request.message
    assert request.reply_markup is None  # no keyboard survives the settle
    assert "<b>Command ran</b>" in request.rich_message.html
    assert ">approval granted<" in request.rich_message.html
    assert 'style="success"' in request.rich_message.html
    assert "ea:settled-approved" not in request.rich_message.html
    assert "Approved by" in request.message and "Kailash" in request.message
    assert f'tg://user?id={OWNER_ID}' in request.message
    assert request.rich_message is not None
    assert answers and answers[-1]["text"] == "Processing approval…"
    assert deletions == [{"delay": 60.0, "chat_id": CHAT_ID, "message_id": 7}]


@pytest.mark.asyncio
async def test_tap_on_settled_badge_answers_the_state_without_alert(adapter, monkeypatch):
    """Tapping a stale settled badge (sent before the badge went
    indicator-only) still answers a plain toast naming the state —
    never the expired-approval alert — and resolves nothing."""
    answers = []

    async def _answer(event, text=None, show_alert=False):
        answers.append({"text": text, "alert": show_alert})

    monkeypatch.setattr(adapter_module, "answer_callback", _answer)
    # No approval state at all: the card was already settled and popped.
    await adapter._handle_exec_approval_callback(_tap_event(), "ea:settled-approved:1")
    assert answers == [{"text": "Command approved", "alert": False}]
    assert adapter._client.sent_messages == []

    await adapter._handle_exec_approval_callback(_tap_event(), "ea:settled-denied:1")
    assert answers[-1] == {"text": "Command denied", "alert": False}
    assert adapter._client.sent_messages == []


@pytest.mark.asyncio
async def test_tap_deny_edits_card_in_place_without_delete(adapter, monkeypatch):
    """A deny edits the same card to the denied state — bold "Command
    denied" plus the red "approval denied" badge on top, no heading, no
    keyboard — and leaves it standing."""
    deletions = []

    async def _answer(event, text=None, show_alert=False):
        return None

    monkeypatch.setattr(adapter_module, "answer_callback", _answer)
    monkeypatch.setattr(
        "plugins.platforms.telegram.message_self_delete.schedule_sent_message_self_delete",
        lambda *a: deletions.append(a))
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", lambda *a, **k: 1)
    adapter._approval_state[1] = _approval_state()

    await adapter._handle_exec_approval_callback(_tap_event(), "ea:deny:1")

    assert len(adapter._client.sent_messages) == 1
    request = adapter._client.sent_messages[0]
    assert "<h1>" not in request.message
    assert "<tg-button-row>" not in request.message
    assert 'style="success"' not in request.message
    assert request.reply_markup is None
    assert "<b>Command denied</b>" in request.rich_message.html
    assert ">approval denied<" in request.rich_message.html
    assert "ea:settled-denied" not in request.rich_message.html
    assert "Denied by" in request.message
    assert deletions == []


@pytest.mark.asyncio
async def test_pending_send_carries_keyboard_and_header_badge(adapter):
    """The pending card goes out as rich HTML with the header badge and a
    real inline keyboard carrying the four actions — no ea: callback data
    in the body, no "Awaiting approval" pill."""
    result = await adapter._send_exec_approval_prompt(_pending_prompt(adapter))

    assert result.success
    assert len(adapter._client.sent_messages) == 1
    request = adapter._client.sent_messages[0]
    assert "Awaiting approval" not in request.message
    assert "(approval pending)" not in request.message
    assert "Hermes needs your ok •" in request.message
    assert ">approval pending<" in request.rich_message.html
    assert 'data="badge:pending"' in request.rich_message.html
    assert 'style="danger"' in request.rich_message.html
    assert "<pre>rm -rf /tmp/stale-cache" in request.message
    assert 'data="ea:' not in request.rich_message.html
    assert request.reply_markup is not None
    assert [len(row.buttons) for row in request.reply_markup.rows] == [2, 2]
    buttons = _keyboard_buttons(request.reply_markup)
    assert [text for text, _data, _style in buttons] == [
        "Allow Once", "Allow Session", "Always allow this pattern", "Deny"]
    assert [data for _text, data, _style in buttons] == [
        b"ea:once:1", b"ea:session:1", b"ea:always:1", b"ea:deny:1"]
    assert request.rich_message is not None
    state = adapter._approval_state[1]
    assert state["command"] == COMMAND  # the decided card records what was approved


# ── Adapter: typed /approve settle ────────────────────────────────────

@pytest.mark.asyncio
async def test_typed_settle_edits_only_the_matching_card(adapter, monkeypatch):
    """Typed settle edits the card whose request id matches, in its own chat only."""
    deletions = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.message_self_delete.schedule_sent_message_self_delete",
        lambda *a: deletions.append(a))
    adapter._approval_state[1] = _approval_state(message_id="11")
    other = _approval_state(message_id="22")
    other["session_key"] = "agent:main:telegram:group:-1009999999999"
    other["chat_id"] = "-1009999999999"
    adapter._approval_state[2] = other

    settled = await adapter.settle_exec_approval_cards(
        "agent:main:telegram:group:-1001234567890", ["req-1"], "once",
        approver_name="Kailash", approver_id=OWNER_ID)

    assert settled == 1
    assert 1 not in adapter._approval_state
    assert 2 in adapter._approval_state  # another session's card is untouched
    assert len(adapter._client.sent_messages) == 1
    request = adapter._client.sent_messages[0]
    assert request.id == 11
    assert "<h1>" not in request.message
    assert "<tg-button-row>" not in request.message
    assert request.reply_markup is None
    assert "<b>Command ran</b>" in request.rich_message.html
    assert ">approval granted<" in request.rich_message.html
    assert "ea:settled-approved" not in request.rich_message.html
    assert deletions and deletions[0][2] == "-1001234567890"

    settled = await adapter.settle_exec_approval_cards(
        "agent:main:telegram:group:-1001234567890", ["req-9"], "once",
        approver_name="Kailash", approver_id=OWNER_ID)
    assert settled == 0
    assert len(adapter._client.sent_messages) == 1  # nothing else was edited


# ── Adapter: timeout settle ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_typed_timeout_settle_edits_card_in_place_without_delete(adapter, monkeypatch):
    """A timeout edits the pending card in place to the timed-out state —
    bold "Command didn't run" plus the red "timed out" badge on top, the
    command, the footer notice — with no keyboard, no auto-delete, and the
    state entry popped. An unknown request settles 0."""
    deletions = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.message_self_delete.schedule_sent_message_self_delete",
        lambda *a: deletions.append(a))
    adapter._approval_state[1] = _approval_state(message_id="11")

    settled = await adapter.settle_exec_approval_cards(
        "agent:main:telegram:group:-1001234567890", ["req-1"], "timeout", timeout_s=300)

    assert settled == 1
    assert 1 not in adapter._approval_state
    assert len(adapter._client.sent_messages) == 1
    request = adapter._client.sent_messages[0]
    assert request.id == 11
    assert "<tg-button-row>" not in request.message
    assert request.reply_markup is None  # no keyboard survives the settle
    assert "<b>Command didn't run</b>" in request.rich_message.html
    assert ">timed out<" in request.rich_message.html
    assert 'data="badge:timeout"' in request.rich_message.html
    assert 'style="danger"' in request.rich_message.html
    assert "<pre>rm -rf /tmp/stale-cache" in request.rich_message.html
    assert "Why it was flagged" not in request.rich_message.html
    assert "Approval timed out after 5 minutes" in request.rich_message.html
    assert deletions == []  # a timeout card stays, like a denied one

    settled = await adapter.settle_exec_approval_cards(
        "agent:main:telegram:group:-1001234567890", ["req-9"], "timeout", timeout_s=300)
    assert settled == 0
    assert len(adapter._client.sent_messages) == 1  # nothing else was edited
