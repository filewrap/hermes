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


@pytest.mark.asyncio
async def test_slash_confirm_owner_tap_in_topic_reaches_handler(monkeypatch, tmp_path):
    """Regression (2026-11-24, live report): /new confirm buttons in a
    forum topic were dead. A Telethon CallbackQuery carries no message
    object (``event.message`` does not exist; only ``get_message()``
    fetches it), so ``_source`` fell back to the General topic
    (thread_id=1) for every forum tap — while the card was created with
    the real topic id. The guard then rejected the tap as belonging to
    another conversation. The handler must read the thread off the tapped
    card's own message."""
    from tests.plugins.platforms.telegram.test_telethon_adapter import (
        _multiuser_guestbot_adapter,
    )
    from tools import slash_confirm

    adapter = _multiuser_guestbot_adapter(monkeypatch, tmp_path, owner_id="7")

    class Client:
        async def __call__(self, request):
            return SimpleNamespace(id=55)

    adapter._client = Client()
    await adapter.send_slash_confirm(
        chat_id="-1001", title="/new",
        message="# Confirm /new\n\nDiscard this session?",
        session_key="session-1", confirm_id="cf-1",
        metadata={"user_id": "7", "thread_id": "456"},
    )
    # The real _source runs here: the event has no message object (like a
    # real CallbackQuery), so it reports the General fallback thread_id=1.
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    actions = []

    async def handle(choice):
        actions.append(choice)
        return "Approved once."

    slash_confirm.register("session-1", "cf-1", "new", handle)
    answers = []
    card_message = SimpleNamespace(
        id=55,
        reply_to=SimpleNamespace(reply_to_top_id=456, forum_topic=True, reply_to_msg_id=456),
    )

    class Event:
        data = b"sc:once:cf-1"
        message_id = 55
        chat_id = "-1001"
        sender = SimpleNamespace(id=7, first_name="K", last_name=None, username=None,
                                 bot=False, scam=False, fake=False)
        chat = SimpleNamespace(broadcast=False, megagroup=True, gigagroup=False,
                               forum=True, title="Test", first_name=None)

        async def answer(self, *args, **kwargs):
            answers.append((args, kwargs))

        async def get_message(self):
            return card_message

    await adapter._on_callback_query(Event())
    assert actions == ["once"]
    assert slash_confirm.get_pending("session-1") is None
    assert ("Processing confirmation…",) in [args for args, _kwargs in answers]


@pytest.mark.asyncio
async def test_slash_confirm_tap_from_other_topic_still_denied(monkeypatch, tmp_path):
    """The topic fix must not open confirms to other topics: a tap whose
    card lives in topic 789 must not resolve a confirm created for topic
    456 — the thread check still binds the tap to the card's topic."""
    from tests.plugins.platforms.telegram.test_telethon_adapter import (
        _multiuser_guestbot_adapter,
    )
    from tools import slash_confirm

    adapter = _multiuser_guestbot_adapter(monkeypatch, tmp_path, owner_id="7")

    class Client:
        async def __call__(self, request):
            return SimpleNamespace(id=55)

    adapter._client = Client()
    await adapter.send_slash_confirm(
        chat_id="-1001", title="/new",
        message="# Confirm /new\n\nDiscard this session?",
        session_key="session-1", confirm_id="cf-1",
        metadata={"user_id": "7", "thread_id": "456"},
    )
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    actions = []

    async def handle(choice):
        actions.append(choice)
        return "Approved once."

    slash_confirm.register("session-1", "cf-1", "new", handle)
    answers = []
    other_topic_card = SimpleNamespace(
        id=55,
        reply_to=SimpleNamespace(reply_to_top_id=789, forum_topic=True, reply_to_msg_id=789),
    )

    class Event:
        data = b"sc:once:cf-1"
        message_id = 55
        chat_id = "-1001"
        sender = SimpleNamespace(id=7, first_name="K", last_name=None, username=None,
                                 bot=False, scam=False, fake=False)
        chat = SimpleNamespace(broadcast=False, megagroup=True, gigagroup=False,
                               forum=True, title="Test", first_name=None)

        async def answer(self, *args, **kwargs):
            answers.append((args, kwargs))

        async def get_message(self):
            return other_topic_card

    await adapter._on_callback_query(Event())
    assert actions == []
    assert answers == [(("This confirmation belongs to another conversation.",),
                        {"alert": True})]
    assert slash_confirm.get_pending("session-1") is not None
