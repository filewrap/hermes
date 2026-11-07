"""GuestBot has no streaming toggle anymore: no streaming row, no stream callbacks.

The guest draft sink was removed 2026-11-07: guestbot cannot send a typing
indicator (the draft never worked live — 0/17 ``PeerIdInvalidError`` on
``SetTypingRequest``). The GuestBot menu keeps only its on/off toggle; the
``hm:guestbot:stream:on|off`` payloads are unhandled, and a legacy
``guest_streaming: true`` in ``config.yaml`` is an inert orphan.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.rich_ui import guestbot_view
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _Client:
    """Fake Telethon client recording requests."""

    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=17)


def test_guestbot_menu_has_no_streaming_row():
    text, markup = guestbot_view(False)
    assert "Guest Mode" in text
    assert "Streaming messages" not in text
    assert "hm:guestbot:stream:" not in text
    assert 'data="hm:guestbot:on"' in text and "Turn on" in text
    keyboard_labels = [button.text for row in markup.rows for button in row.buttons]
    assert keyboard_labels == ["Back to help"]

    text, _markup = guestbot_view(True)
    assert "Guest Mode" in text
    assert "Streaming messages" not in text
    assert "hm:guestbot:stream:" not in text
    assert 'data="hm:guestbot:off"' in text and "Turn off" in text


def test_guestbot_menu_ignores_legacy_streaming_flag():
    """A persisted ``guest_streaming: true`` must not bring the row back."""
    adapter = TelethonAdapter(PlatformConfig(extra={"guest_streaming": True}))

    text, _markup = adapter._guestbot_menu()

    assert "Streaming messages" not in text
    assert "hm:guestbot:stream:" not in text


def _multiuser_guestbot_adapter(monkeypatch, tmp_path, *, owner_id="7"):
    """An adapter with multiuser mode on, so the callback gate applies."""
    import gateway.mongo_multiuser as multiuser

    monkeypatch.setattr(multiuser, "enabled", lambda _home: True)
    monkeypatch.setattr(multiuser, "owner_user_id", lambda _home: owner_id)
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
    return adapter


def _guestbot_event(data: bytes, message_id: int = 17):
    answered = []

    class Event:
        async def answer(self, *args, **kwargs):
            answered.append((args, kwargs))

    event = Event()
    event.data = data
    event.message_id = message_id
    return event, answered


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"hm:guestbot:stream:on", b"hm:guestbot:stream:off"])
async def test_guestbot_stream_callback_is_unavailable(monkeypatch, tmp_path, payload):
    """The removed stream payloads are no longer a control: the tap is refused
    as unavailable, nothing is persisted, and no edit goes out."""
    adapter = _multiuser_guestbot_adapter(monkeypatch, tmp_path)
    source = SimpleNamespace(chat_id="999", user_id="999", chat_type="dm")
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    client = _Client()
    adapter._client = client
    event, answered = _guestbot_event(payload)

    await adapter._on_callback_query(event)

    assert answered == [(("This Telegram action is unavailable.",), {"alert": True})]
    assert not client.requests
    config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8") if (tmp_path / "config.yaml").exists() else ""
    assert "guest_streaming" not in config_text


def _guest_event(*, thread_id="guest-77"):
    """A real MessageEvent on a guest thread (or a plain thread when given)."""
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="77", thread_id=thread_id)
    return MessageEvent(text="Can you help?", source=source, message_id="500")


def _typing_requests(client):
    return [r for r in client.requests if type(r).__name__ == "SetTypingRequest"]


async def _cancel(task):
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_start_typing_refresh_returns_none_for_guest_thread():
    """A guest turn must not spawn the generic typing refresh: the plain
    typing action fails exactly like the removed draft did (0/17
    ``PeerIdInvalidError`` on ``SetTypingRequest``), so no task at all."""
    adapter = TelethonAdapter(PlatformConfig(extra={}))

    task = adapter._start_typing_refresh(_guest_event(), asyncio.Event(), None)
    try:
        assert task is None
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_start_typing_refresh_still_spawns_for_non_guest_thread(monkeypatch):
    """The gate is guest-only: a normal turn still gets its typing refresh,
    and the refresh actually ticks."""
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    calls = []

    async def fake_send_typing(chat_id, metadata=None):
        calls.append(chat_id)

    monkeypatch.setattr(adapter, "send_typing", fake_send_typing)

    task = adapter._start_typing_refresh(_guest_event(thread_id=None), asyncio.Event(), None)
    assert isinstance(task, asyncio.Task)
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert calls == ["77"]
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_guest_producer_bracket_emits_no_typing_requests():
    """A guest turn through the producer bracket emits zero ``SetTypingRequest``
    of any kind — no draft action, no plain typing action — while the turn
    itself still runs and its Thinking… status message still goes out. (The
    final delivery beside it is covered by the gateway guest-query test.)"""
    from gateway.session_producer_lifecycle import producer_turn
    from plugins.platforms.telegram.guestbot import send_guest_status_message

    client = _Client()
    sent = []

    async def send_message(peer, text, **kwargs):
        sent.append((peer, text, kwargs))
        return SimpleNamespace(id=501)

    client.send_message = send_message
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = client
    adapter._peer_cache["77"] = SimpleNamespace()
    event = _guest_event()
    authority = SimpleNamespace(
        producer_turns={"adm-1": None},
        runner=SimpleNamespace(_adapter_for_source=lambda _source: adapter),
    )
    row = {"admission_id": "adm-1", "status": "queued"}
    ran = []

    async with producer_turn(authority, row, event, {}):
        ran.append(True)
        status_id = await send_guest_status_message(adapter, "77", reply_to_message_id=500)
        # A leaked refresh task fires its first tick on the next loop turn.
        for _ in range(10):
            await asyncio.sleep(0)
        # Checked inside the bracket: stop_typing() clears it on exit, so a
        # post-exit read cannot tell a spawned task from none at all.
        assert authority.producer_turns["adm-1"].typing is None

    assert ran == [True]
    assert status_id == "501"
    assert [text for _peer, text, _kw in sent] == ["Thinking…"]
    assert _typing_requests(client) == []
