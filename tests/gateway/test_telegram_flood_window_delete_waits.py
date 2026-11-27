"""The delete wait loop: a window re-armed while it sleeps is re-checked, never slept inline past the
cap, and a window that closes during the last allowed wait still gets its delete.

Telethon-native: the loop under test is ``telegram_flood_window.delete_outside_flood_window``,
the seam the Telethon adapter's delete path uses (a ``delete`` callable doing the MTProto
``delete_messages`` call), driven here by a fake adapter carrying the cooldown state. The old
file drove the PTB adapter's ``delete_message`` (``_bot.delete_message``, ``TelegramAdapter``);
that adapter does not exist in the fork.
"""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram import telegram_flood_window as fw
from plugins.platforms.telegram.adapter import _FLOOD_INLINE_WAIT_CAP_SECS
from plugins.platforms.telegram.telegram_media_flood import _MAX_WINDOW_WAITS

_real_sleep = asyncio.sleep


class _FakeAdapter:
    """Cooldown state only: the wait loop reads it through the two methods below."""

    name = "telegram"

    def __init__(self):
        self._cooldown_until: dict[str, float] = {}
        self._background_tasks: set = set()

    def _record_send_flood_cooldown(self, chat_id, wait):
        self._cooldown_until[str(chat_id)] = time.monotonic() + float(wait)

    def _send_flood_cooldown_remaining(self, chat_id):
        until = self._cooldown_until.get(str(chat_id))
        if until is None:
            return None
        remaining = until - time.monotonic()
        if remaining <= 0:
            del self._cooldown_until[str(chat_id)]
            return None
        return remaining

    async def cancel_background_tasks(self):
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)


def _scripted_sleep(monkeypatch, on_wake):
    """Each sleep the delete loop takes returns at once; ``on_wake(n)`` is what other in-flight
    traffic did to the window meanwhile."""
    slept = []

    async def sleep(seconds, *a, **k):
        if seconds:
            slept.append(seconds)
            on_wake(len(slept))
        await _real_sleep(0)
    monkeypatch.setattr(fw.asyncio, "sleep", sleep)
    return slept


@pytest.mark.asyncio
async def test_a_window_closing_during_the_last_allowed_wait_still_deletes(monkeypatch):
    adapter = _FakeAdapter()
    adapter._record_send_flood_cooldown("4242", 1.0)
    delete = AsyncMock(return_value=True)

    def wake(n):
        if n <= _MAX_WINDOW_WAITS:
            adapter._record_send_flood_cooldown("4242", 1.0)  # re-armed by a sibling request
        else:
            adapter._cooldown_until.clear()  # closed during the last sleep
    slept = _scripted_sleep(monkeypatch, wake)

    assert await fw.delete_outside_flood_window(adapter, "4242", delete) is True
    assert len(slept) == _MAX_WINDOW_WAITS + 1
    delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_window_extended_during_an_inline_wait_moves_to_the_background(monkeypatch):
    adapter = _FakeAdapter()
    adapter._record_send_flood_cooldown("4242", 1.0)  # short: waited inline
    delete = AsyncMock(return_value=True)
    slept = _scripted_sleep(monkeypatch, lambda n: adapter._record_send_flood_cooldown("4242", 240.0))

    assert await fw.delete_outside_flood_window(adapter, "4242", delete) is True  # owned by the background task now
    assert len(slept) == 1 and slept[0] <= _FLOOD_INLINE_WAIT_CAP_SECS  # never slept the long window inline
    assert delete.await_count == 0
    assert [t for t in adapter._background_tasks if not t.done()]
    await adapter.cancel_background_tasks()
