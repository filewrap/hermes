"""Behaviour contracts for the 5s self-deleting unknown-command message.

The helper is Ultroid's eor/try_delete pattern, minimal: one async
``delete_after`` patched onto Telethon's Message, plus a scheduler the
adapter's ``send`` calls when the metadata carries
``telegram_self_delete_after_s``. Every failure is swallowed — the delete
must never crash the turn.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types
from telethon.tl.custom.message import Message

from gateway.config import PlatformConfig
from plugins.platforms.telegram.message_self_delete import (
    SELF_DELETE_METADATA_KEY,
    delete_after,
    install_message_self_delete,
    schedule_sent_message_self_delete,
)
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _FakeMessage:
    def __init__(self, fail=False):
        self.deleted = False
        self._fail = fail
        self.delete_after_calls = []

    async def delete(self):
        if self._fail:
            raise RuntimeError("cannot delete")
        self.deleted = True

    async def delete_after(self, delay=5.0):
        self.delete_after_calls.append(delay)
        await delete_after(self, delay=delay)


@pytest.mark.asyncio
async def test_delete_after_deletes_and_never_raises():
    message = _FakeMessage()
    await delete_after(message, delay=0)
    assert message.deleted is True

    # A delete that fails (already gone, no rights) is swallowed, never raised.
    failing = _FakeMessage(fail=True)
    await delete_after(failing, delay=0)


def test_install_message_self_delete_patches_message():
    install_message_self_delete()
    assert callable(Message.delete_after)


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = AsyncMock()
    adapter._peer_for = lambda chat_id: types.InputPeerSelf()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerSelf())
    adapter._request_with_bounded_retry = AsyncMock(
        return_value=SimpleNamespace(id=777))
    adapter._sent_message_id = staticmethod(
        lambda response, **kw: str(response.id))
    return adapter


@pytest.mark.asyncio
async def test_send_with_self_delete_metadata_schedules_deletion(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    install_message_self_delete()
    fake = _FakeMessage()
    adapter._client.get_messages = AsyncMock(return_value=fake)

    result = await adapter.send(
        "c1", "hi", metadata={SELF_DELETE_METADATA_KEY: 0.01})

    assert result.success is True
    assert result.message_id == "777"
    tasks = list(getattr(adapter, "_background_tasks", set()))
    assert len(tasks) == 1
    await asyncio.wait_for(tasks[0], timeout=5)
    assert fake.delete_after_calls == [0.01]
    assert fake.deleted is True


def test_schedule_self_delete_ignores_missing_or_non_positive_delay():
    adapter = SimpleNamespace(_background_tasks=set())
    assert schedule_sent_message_self_delete(adapter, {}, "c1", "777") is None
    assert schedule_sent_message_self_delete(
        adapter, {SELF_DELETE_METADATA_KEY: 0}, "c1", "777") is None
    assert schedule_sent_message_self_delete(
        adapter, {SELF_DELETE_METADATA_KEY: -1}, "c1", "777") is None
    assert schedule_sent_message_self_delete(
        adapter, {SELF_DELETE_METADATA_KEY: 5}, "c1", None) is None
    assert adapter._background_tasks == set()


@pytest.mark.asyncio
async def test_send_without_self_delete_metadata_schedules_nothing(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)

    result = await adapter.send("c1", "hi")

    assert result.success is True
    assert list(getattr(adapter, "_background_tasks", set())) == []


@pytest.mark.asyncio
async def test_self_delete_falls_back_to_delete_by_id_when_fetch_fails(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter._client.get_messages = AsyncMock(return_value=None)
    adapter.delete_message = AsyncMock(return_value=True)

    result = await adapter.send(
        "c1", "hi", metadata={SELF_DELETE_METADATA_KEY: 0.01})

    assert result.success is True
    tasks = list(getattr(adapter, "_background_tasks", set()))
    assert len(tasks) == 1
    await asyncio.wait_for(tasks[0], timeout=5)
    adapter.delete_message.assert_awaited_once_with("c1", "777")
