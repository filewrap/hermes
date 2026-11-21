"""Telethon port of upstream's callback-answer contract: a tap on an expired
card reports False instead of raising out of the handler; any other answer
failure still raises; an expired-menu tap falls back to a chat note."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors.rpcerrorlist import QueryIdInvalidError

from plugins.platforms.telegram import callback_answer as ca


def _event(*, side_effect=None):
    event = SimpleNamespace(
        chat_id="4242",
        message=SimpleNamespace(chat_id="4242"),
        answer=AsyncMock(side_effect=side_effect),
    )
    return event


@pytest.mark.asyncio
async def test_answer_callback_success():
    event = _event()
    assert await ca.answer_callback(event, "ok") is True
    event.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_answer_callback_expired_query_returns_false():
    event = _event(side_effect=QueryIdInvalidError(request=None))
    assert await ca.answer_callback(event, "ok") is False


@pytest.mark.asyncio
async def test_answer_callback_other_error_raises():
    event = _event(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await ca.answer_callback(event, "ok")


def test_is_stale_query_error_matches_telethon_type_and_wording():
    assert ca.is_stale_query_error(QueryIdInvalidError(request=None)) is True
    assert ca.is_stale_query_error(RuntimeError("query id is invalid")) is False
    assert ca.is_stale_query_error(RuntimeError("boom")) is False


@pytest.mark.asyncio
async def test_answer_or_note_posts_in_chat_when_answer_refused():
    event = _event(side_effect=QueryIdInvalidError(request=None))
    adapter = SimpleNamespace(
        _message_thread_id=lambda _m: "77",
        send=AsyncMock(return_value=None),
    )
    await ca.answer_or_note(adapter, event, "Picker expired — use /model again.")
    adapter.send.assert_awaited_once_with(
        "4242", "Picker expired — use /model again.", metadata={"thread_id": "77"}
    )


@pytest.mark.asyncio
async def test_answer_or_note_skips_note_when_answer_succeeds():
    event = _event()
    adapter = SimpleNamespace(_message_thread_id=lambda _m: None, send=AsyncMock())
    await ca.answer_or_note(adapter, event, "Picker expired — use /model again.")
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_refuse_tap_without_requester_answers_nothing():
    event = _event()
    assert await ca.refuse_tap(event, None) is False
    event.answer.assert_not_awaited()
