"""Slash-confirm card tests for the Telethon adapter.

Moved out of test_telethon_adapter.py so that file stays under its health
cap. The confirm card renders as a minimal rich-HTML card (title + one short
line, text fallbacks in an italic footer) with the three choices as a
regular inline keyboard, and each tap resolves exactly once.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.mark.asyncio
async def test_slash_confirm_uses_native_mtproto_buttons_and_one_shot_callback(monkeypatch):
    from tools import slash_confirm

    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=55)

    adapter._client = Client()
    sent = await adapter.send_slash_confirm(
        chat_id="42", title="/new",
        message=("# Confirm /new\n\nDiscard this session?\n\n"
                 "_Text fallback: reply `/approve`, `/always`, or `/cancel`._"),
        session_key="session-1", confirm_id="cf-1",
        metadata={"user_id": "7", "thread_id": "77", "reply_to": "88"},
    )
    assert sent.success
    request = calls[0]
    assert type(request).__name__ == "SendMessageRequest"
    assert request.reply_to.reply_to_msg_id == 88
    assert request.reply_to.top_msg_id == 77
    # Minimal card: title + one short line, the text fallbacks in an italic
    # footer, the three choices as a regular inline keyboard — no in-body
    # badges, no "Always approve saves..." explainer, no copy buttons.
    assert type(request.rich_message).__name__ == "InputRichMessageHTML"
    html = request.rich_message.html
    assert "Confirm /new" in html
    assert "Discard this session?" in html
    assert "profile-wide preference" not in html
    assert "<i>Text fallback:" in html
    assert "/approve" in html and "/always" in html and "/cancel" in html
    assert "<tg-button" not in html
    assert 'data="sc:' not in html
    assert request.reply_markup is not None
    rows = [[button for button in row.buttons] for row in request.reply_markup.rows]
    assert [len(row) for row in rows] == [1, 2]
    assert [button.text for row in rows for button in row] == [
        "Approve once", "Always approve", "Cancel",
    ]
    assert [button.type.data for row in rows for button in row] == [
        b"sc:once:cf-1", b"sc:always:cf-1", b"sc:cancel:cf-1",
    ]
    assert rows[0][0].style.bg_success is True
    assert not any(icon in html for icon in ("⚠️", "✅", "❌", "ℹ️", "🟡"))

    source = SimpleNamespace(chat_id="42", user_id="7", thread_id="77")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    actions = []

    async def handle(choice):
        actions.append(choice)
        return "Approved once."

    slash_confirm.register("session-1", "cf-1", "new", handle)
    answers = []

    async def answer(text=None, alert=False):
        answers.append((text or "", alert))

    event = SimpleNamespace(message_id=55, answer=answer)
    await adapter._handle_slash_confirm_callback(event, "sc:once:cf-1")
    assert actions == ["once"]
    assert slash_confirm.get_pending("session-1") is None
    assert type(calls[-1]).__name__ == "EditMessageRequest"
    assert calls[-1].rich_message.markdown == "Approved once."
    assert calls[-1].reply_markup is None

    await adapter._handle_slash_confirm_callback(event, "sc:once:cf-1")
    assert actions == ["once"]
    assert answers[-1][1] is True
    assert "cf-1" not in adapter._slash_confirm_state


@pytest.mark.asyncio
async def test_slash_confirm_approve_edits_card_once_with_result(monkeypatch):
    from tools import slash_confirm

    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=55)

    adapter._client = Client()
    sent = await adapter.send_slash_confirm(
        chat_id="42", title="/new",
        message=("# Confirm /new\n\nDiscard this session?\n\n"
                 "**Text fallback:** reply `/approve`, `/always`, or `/cancel`."),
        session_key="session-2", confirm_id="cf-2",
        metadata={"user_id": "7", "thread_id": "77", "reply_to": "88"},
    )
    assert sent.success

    source = SimpleNamespace(chat_id="42", user_id="7", thread_id="77")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)

    async def handle(choice):
        return "Approved once."

    slash_confirm.register("session-2", "cf-2", "new", handle)
    answers = []

    async def answer(text=None, alert=False):
        answers.append((text or "", alert))

    event = SimpleNamespace(message_id=55, answer=answer)
    await adapter._handle_slash_confirm_callback(event, "sc:once:cf-2")

    # One edit only, carrying the final result: no Working… progress edit
    # before it, so the card is not edited twice.
    assert [type(call).__name__ for call in calls] == ["SendMessageRequest", "EditMessageRequest"]
    assert calls[-1].rich_message.markdown == "Approved once."
    assert calls[-1].reply_markup is None
    assert ("Processing confirmation…", False) in answers


@pytest.mark.asyncio
async def test_slash_confirm_cancel_edits_card_once(monkeypatch):
    from tools import slash_confirm

    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=55)

    adapter._client = Client()
    sent = await adapter.send_slash_confirm(
        chat_id="42", title="/new",
        message=("# Confirm /new\n\nDiscard this session?\n\n"
                 "**Text fallback:** reply `/approve`, `/always`, or `/cancel`."),
        session_key="session-3", confirm_id="cf-3",
        metadata={"user_id": "7", "thread_id": "77", "reply_to": "88"},
    )
    assert sent.success

    source = SimpleNamespace(chat_id="42", user_id="7", thread_id="77")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)

    async def handle(choice):
        return "The action was cancelled."

    slash_confirm.register("session-3", "cf-3", "new", handle)
    answers = []

    async def answer(text=None, alert=False):
        answers.append((text or "", alert))

    event = SimpleNamespace(message_id=55, answer=answer)
    await adapter._handle_slash_confirm_callback(event, "sc:cancel:cf-3")

    assert [type(call).__name__ for call in calls] == ["SendMessageRequest", "EditMessageRequest"]
    assert calls[-1].rich_message.markdown == "The action was cancelled."
    assert calls[-1].reply_markup is None
    assert ("Processing confirmation…", False) in answers
