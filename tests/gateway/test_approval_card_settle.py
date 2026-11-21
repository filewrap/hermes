"""Typed /approve and /deny settle the pending approval card in place.

``settle_approval_cards`` (gateway/approval_actor.py) edits the card the
delivery adapter sent instead of posting a new confirmation message; when
the adapter has no such seam — or answers with anything but an int count —
it settles nothing and the caller falls back to the text confirmation, so
an answer is never swallowed.
"""

import itertools
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.approval_actor import deliver_approval_confirmation, settle_approval_cards
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

CHAT_ID = "-1001234567890"
OWNER_ID = "6802929470"
SESSION_KEY = "agent:main:telegram:group:-1001234567890"


class FakeClient:
    def __init__(self):
        self.sent_messages = []

    async def __call__(self, request):
        self.sent_messages.append(request)
        return SimpleNamespace(id=101)


def _adapter_with_card():
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    adapter._client = FakeClient()
    adapter.config = SimpleNamespace(extra={})
    adapter._approval_state = {
        1: {
            "session_key": SESSION_KEY,
            "request_id": "req-1",
            "chat_id": CHAT_ID,
            "thread_id": None,
            "profile_home": "/tmp/test-home",
            "owner_user_id": OWNER_ID,
            "intended_user_id": OWNER_ID,
            "owner_session": True,
            "allowed_choices": {"once", "session", "always", "deny"},
            "created_at": time.monotonic(),
            "message_id": "7",
            "command": "rm -rf /tmp/stale-cache",
        },
    }
    adapter._approval_counter = itertools.count(2)
    adapter._peer_for = lambda _chat_id: f"peer:{_chat_id}"
    adapter._sent_message_id = lambda res, **kw: str(res.id)
    return adapter


def _runner_with(adapter):
    return SimpleNamespace(
        _delivery_adapter_for=lambda _source: adapter,
        _session_key_for_source=lambda _source: SESSION_KEY,
    )


def _event():
    source = SimpleNamespace(user_id=OWNER_ID, user_name="Kailash", chat_id=CHAT_ID)
    return SimpleNamespace(source=source, message_id="m1",
                           sender_id=lambda: OWNER_ID)


@pytest.mark.asyncio
async def test_typed_approve_settles_the_card(monkeypatch):
    """A typed approval edits the pending card (no new message) and returns nothing to send."""
    deletions = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.message_self_delete.schedule_sent_message_self_delete",
        lambda *a: deletions.append(a))
    adapter = _adapter_with_card()
    runner = _runner_with(adapter)

    settled = await settle_approval_cards(runner, _event(), ["req-1"], "once")

    assert settled == 1
    assert 1 not in adapter._approval_state
    assert len(adapter._client.sent_messages) == 1
    request = adapter._client.sent_messages[0]
    assert "<h1>" not in request.message
    assert "<tg-button-row>" not in request.message
    # The settled edit carries no keyboard: a text-only edit clears the
    # pending one. The state rides in the line itself — bold lead plus the
    # granted badge answering its toast on tap.
    assert request.reply_markup is None
    assert "Command ran" in request.message
    assert "approval granted" in request.message
    assert 'data="badge:granted"' in request.message
    assert "Approved by" in request.message
    assert deletions and deletions[0][2] == CHAT_ID


@pytest.mark.asyncio
async def test_typed_approve_falls_back_to_text_without_a_real_seam():
    """A mock adapter (or any seam that does not answer an int count) settles
    nothing, so the typed confirmation text still goes out."""
    runner = _runner_with(MagicMock())
    event = _event()

    settled = await settle_approval_cards(runner, event, ["req-1"], "once")
    assert settled == 0

    result = await deliver_approval_confirmation(
        runner, event, "**Approved 2 commands.**", "approve", ["req-1"], "once")
    assert result == "**Approved 2 commands.**"
