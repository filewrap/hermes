"""Badge taps answer a short toast per type via the ``badge:<type>`` template.

The badge is a status indicator: tapping it never acts, only answers.
"""

from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.callback_answer import answer_badge_callback
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _Event:
    """Minimal Telethon callback-query event: records every answer."""

    def __init__(self, data):
        self.data = data
        self.message_id = 101
        self.chat_id = 1
        self.sender_id = 1
        self.sender = SimpleNamespace(id=1, first_name="Alice", bot=False)
        self.chat = SimpleNamespace(id=1, first_name="Alice")
        self.message = SimpleNamespace(id=101, chat_id=1, reply_to=None)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


def _adapter():
    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="t"))

    class Client:
        async def __call__(self, request):
            return SimpleNamespace(id=101)

    adapter._client = Client()
    return adapter


@pytest.mark.asyncio
async def test_badge_pending_answers_sad_pending():
    event = _Event("badge:pending")
    assert await answer_badge_callback(event, "badge:pending") is True
    assert event.answers == [((":( pending",), {})]


@pytest.mark.asyncio
async def test_badge_granted_answers_happy_granted():
    event = _Event("badge:granted")
    assert await answer_badge_callback(event, "badge:granted") is True
    assert event.answers == [((":) granted",), {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,toast", [
    ("new", "new function — enjoy"),
    ("beta", "beta function — enjoy"),
])
async def test_badge_existing_kinds_keep_their_toasts(kind, toast):
    event = _Event(f"badge:{kind}")
    assert await answer_badge_callback(event, f"badge:{kind}") is True
    assert event.answers == [((toast,), {})]


@pytest.mark.asyncio
async def test_badge_timeout_answers_sad_timed_out():
    event = _Event("badge:timeout")
    assert await answer_badge_callback(event, "badge:timeout") is True
    assert event.answers == [((":( timed out",), {})]


@pytest.mark.asyncio
async def test_badge_chat_answers_chat_automation():
    """The chat badge (business-origin messages) answers its toast."""
    event = _Event("badge:chat")
    assert await answer_badge_callback(event, "badge:chat") is True
    assert event.answers == [((":) chat automation",), {})]


@pytest.mark.asyncio
async def test_badge_unknown_type_answers_empty_without_crashing():
    event = _Event("badge:nope")
    assert await answer_badge_callback(event, "badge:nope") is True
    assert event.answers == [((), {})]


@pytest.mark.asyncio
async def test_badge_pending_tap_dispatched_through_callback_query():
    adapter = _adapter()
    event = _Event("badge:pending")
    await adapter._on_callback_query(event)
    assert event.answers == [((":( pending",), {})]


@pytest.mark.asyncio
async def test_badge_granted_tap_dispatched_through_callback_query():
    adapter = _adapter()
    event = _Event("badge:granted")
    await adapter._on_callback_query(event)
    assert event.answers == [((":) granted",), {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,toast", [
    ("new", "new function — enjoy"),
    ("beta", "beta function — enjoy"),
])
async def test_badge_settings_tap_dispatched_through_callback_query(kind, toast):
    adapter = _adapter()
    event = _Event(f"badge:{kind}")
    await adapter._on_callback_query(event)
    assert event.answers == [((toast,), {})]


@pytest.mark.asyncio
async def test_badge_timeout_tap_dispatched_through_callback_query():
    adapter = _adapter()
    event = _Event("badge:timeout")
    await adapter._on_callback_query(event)
    assert event.answers == [((":( timed out",), {})]


@pytest.mark.asyncio
async def test_badge_unknown_tap_dispatched_without_toast_or_crash():
    adapter = _adapter()
    event = _Event("badge:nope")
    await adapter._on_callback_query(event)
    assert event.answers == [((), {})]
