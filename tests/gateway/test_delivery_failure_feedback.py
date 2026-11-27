"""A terminal final-send failure feeds back to the agent and the user.

When a final reply is refused for good (a rich payload Telegram rejects, not a
transient network blip or a flood penalty), the turn used to end as if the
reply had landed: the transcript already held the answer, the delivery ledger
re-sent the same doomed payload, and nobody was told. Now the failure is
recorded as a one-shot note the agent sees on its next turn, and the user gets
one short MTProto notice — never a Bot API downgrade, never the failed content
re-sent.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform, PlatformConfig
from gateway.delivery_failure_feedback import (
    build_delivery_failure_note,
    record_delivery_failure_note,
    terminal_delivery_failure,
)
from gateway.platforms.base import SendResult

SESSION_KEY = "agent:main:telegram:dm:5230977008"
CHAT = "5230977008"
INBOUND_ID = "5301"
TEXT = "**Yes.** I would make room for one island trip and one Balkan trip."

BAD_REQUEST = SendResult(
    success=False,
    error="Telegram MTProto send failed: BadRequestError",
    raw_response={"error_detail": "RPCError 400: RICH_MESSAGE_INVALID (caused by SendMessageRequest)"},
)


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    monkeypatch.setattr(dl, "_owner_stamp", lambda: (os.getpid(), 202))
    monkeypatch.setattr(dl, "ledger_enabled", lambda config=None: True)
    yield


def _source(*, chat_id=CHAT, thread_id=None, chat_type="dm"):
    return SimpleNamespace(platform=Platform.TELEGRAM, chat_id=chat_id, thread_id=thread_id,
                           chat_type=chat_type, parent_chat_id=None)


def _event(text=TEXT):
    return SimpleNamespace(source=_source(), text=text, message_id=INBOUND_ID)


def _telegram_adapter(send_result=None):
    """A real Telegram adapter (the base contract) whose transport send is a mock."""
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    runner = MagicMock()
    runner._delivery_adapter_for = MagicMock(return_value=adapter)
    runner._schedule_flood_redelivery = MagicMock(return_value=187.0)
    runner._pending_delivery_failure_notes = {}
    adapter.gateway_runner = runner
    adapter.send = AsyncMock(return_value=send_result or SendResult(success=True, message_id="900"))
    adapter.edit_message = AsyncMock(
        return_value=SendResult(success=False, error="message to edit not found"))
    return adapter


async def _deliver(adapter, *, text=TEXT):
    return await adapter.send_final_ledgered(
        _event(text), SESSION_KEY, text, {}, reply_to=INBOUND_ID, is_ephemeral_response=False)


# ---------------------------------------------------------------------------
# The gate: only a definitive, non-transient failure feeds back.
# ---------------------------------------------------------------------------

def test_gate_definitive_bad_request_feeds_back():
    assert terminal_delivery_failure(BAD_REQUEST)


def test_gate_success_partial_flood_dead_and_timeout_do_not():
    assert not terminal_delivery_failure(SendResult(success=True, message_id="1"))
    assert not terminal_delivery_failure(SendResult(
        success=False, error="partial", raw_response={"partial_overflow": True}))
    assert not terminal_delivery_failure(SendResult(
        success=False, error="FloodWait: retry after 60s", retry_after=60.0))
    assert not terminal_delivery_failure(SendResult(
        success=False, error="Forbidden: bot was blocked by the user"))
    assert not terminal_delivery_failure(SendResult(
        success=False, error="ReadTimeout: request timed out"))
    assert not terminal_delivery_failure(SendResult(success=False, error=""))


# ---------------------------------------------------------------------------
# The note: recorded once, consumed once, never replayed as user text.
# ---------------------------------------------------------------------------

def test_note_states_the_reply_never_arrived_with_its_reason():
    note = build_delivery_failure_note(reason=BAD_REQUEST.raw_response["error_detail"], chars=1609)
    assert "not delivered" in note
    assert "RICH_MESSAGE_INVALID" in note
    assert "1609" in note
    assert "never saw" in note


def test_pending_note_is_consumed_by_the_next_turn():
    from gateway.run_turn_runner import TurnRunner

    note = "[Note: your previous reply was not delivered.]"
    stub = SimpleNamespace(
        _ctx=SimpleNamespace(session_key=SESSION_KEY, message="hello"),
        _runner=SimpleNamespace(_pending_delivery_failure_notes={SESSION_KEY: note}))
    TurnRunner._prepend_pending_note(stub, "_pending_delivery_failure_notes")
    assert stub._ctx.message == note + "\n\nhello"
    assert SESSION_KEY not in stub._runner._pending_delivery_failure_notes


def test_record_note_ignores_a_missing_runner_or_session():
    record_delivery_failure_note(None, SESSION_KEY, "x")
    record_delivery_failure_note(SimpleNamespace(), "", "x")


# ---------------------------------------------------------------------------
# The bracket: terminal failure -> note on the runner + one notice to the user.
# ---------------------------------------------------------------------------

class TestFinalSendFailureFeedback:
    @pytest.mark.asyncio
    async def test_terminal_failure_writes_note_and_notifies_user(self):
        adapter = _telegram_adapter()
        adapter.send = AsyncMock(side_effect=[
            BAD_REQUEST,
            SendResult(success=True, message_id="901"),
        ])

        result, _ = await _deliver(adapter)

        assert result.success is False
        notes = adapter.gateway_runner._pending_delivery_failure_notes
        assert isinstance(notes, dict)
        note = notes[SESSION_KEY]
        assert "not delivered" in note and "RICH_MESSAGE_INVALID" in note
        assert adapter.send.await_count == 2
        notice_text = adapter.send.await_args_list[1].kwargs.get("content", "")
        assert "could not be delivered" in notice_text
        assert "RICH_MESSAGE_INVALID" in notice_text
        # The failed content itself is never re-sent.
        assert TEXT not in notice_text

    @pytest.mark.asyncio
    async def test_success_sends_no_note_and_no_notice(self):
        adapter = _telegram_adapter()

        result, _ = await _deliver(adapter)

        assert result.success is True
        assert adapter.send.await_count == 1
        assert adapter.gateway_runner._pending_delivery_failure_notes == {}

    @pytest.mark.asyncio
    async def test_flood_failure_sends_no_note_and_no_notice(self):
        adapter = _telegram_adapter()
        adapter.send = AsyncMock(return_value=SendResult(
            success=False, error="FloodWait: retry after 60s", retry_after=60.0))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result, _ = await _deliver(adapter)

        assert result.success is False
        assert adapter.send.await_count == 3  # initial + 2 retries, all refused
        assert adapter.gateway_runner._pending_delivery_failure_notes == {}

    @pytest.mark.asyncio
    async def test_dead_target_sends_no_note_and_no_notice(self):
        adapter = _telegram_adapter()
        adapter.send = AsyncMock(return_value=SendResult(
            success=False, error="Forbidden: bot was blocked by the user"))

        result, _ = await _deliver(adapter)

        assert result.success is False
        assert adapter.send.await_count == 1
        assert adapter.gateway_runner._pending_delivery_failure_notes == {}

    @pytest.mark.asyncio
    async def test_notice_failure_is_swallowed_and_the_turn_still_ends(self):
        adapter = _telegram_adapter()
        adapter.send = AsyncMock(side_effect=[
            BAD_REQUEST,
            SendResult(success=False, error="Telegram MTProto send failed: BadRequestError"),
        ])

        result, _ = await _deliver(adapter)

        assert result.success is False
        assert adapter.send.await_count == 2
        assert SESSION_KEY in adapter.gateway_runner._pending_delivery_failure_notes
