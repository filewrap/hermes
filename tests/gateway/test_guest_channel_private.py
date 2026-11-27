"""GuestBot ChannelPrivateError is expected noise, never an error.

A guest turn's answer also goes out via the guest result, so the normal
send failing with ChannelPrivateError is normal — the guest still gets the
answer through the guest result. The adapter must log that failure at
debug (not warning) for a guest thread, keep warning for every other
failure, and treat ChannelPrivateError as a final refusal (no retry, no
plain-text fallback) so the gateway does not log a second warning.
"""
import logging
from types import SimpleNamespace

import pytest
from telethon.errors import ChannelPrivateError

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.guestbot import is_ignored_guest_send_error
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _adapter():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._client = SimpleNamespace()
    adapter._peer_cache["77"] = SimpleNamespace()
    return adapter


def test_ignored_guest_send_error_only_guest_channel_private():
    err = ChannelPrivateError(request=SimpleNamespace())
    assert is_ignored_guest_send_error(err, {"thread_id": "guest-77-123"}) is True
    assert is_ignored_guest_send_error(err, {"thread_id": "other-77"}) is False
    assert is_ignored_guest_send_error(err, {}) is False
    assert is_ignored_guest_send_error(RuntimeError("boom"), {"thread_id": "guest-77-123"}) is False


def test_send_retry_is_final_for_channel_private():
    adapter = _adapter()
    assert adapter._send_retry_is_final(
        SendResult(success=False, error="Telegram MTProto send failed: ChannelPrivateError")) is True
    assert adapter._send_retry_is_final(
        SendResult(success=False, error="Telegram MTProto send failed: FloodWaitError")) is False
    assert adapter._send_retry_is_final(
        SendResult(success=False, error="some other failure")) is False


@pytest.mark.asyncio
async def test_guest_channel_private_send_logs_debug_not_warning(monkeypatch, caplog):
    adapter = _adapter()

    async def _raise_part(request, *, progress_card):
        raise ChannelPrivateError(request=SimpleNamespace())

    monkeypatch.setattr(adapter, "_send_part", _raise_part)
    with caplog.at_level(logging.DEBUG, logger="plugins.platforms.telegram.telethon_adapter"):
        result = await adapter.send("77", "hello guest", metadata={"thread_id": "guest-77-123"})
    assert result.success is False
    assert "ChannelPrivateError" in (result.error or "")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_non_guest_channel_private_send_still_warns(monkeypatch, caplog):
    adapter = _adapter()

    async def _raise_part(request, *, progress_card):
        raise ChannelPrivateError(request=SimpleNamespace())

    monkeypatch.setattr(adapter, "_send_part", _raise_part)
    with caplog.at_level(logging.DEBUG, logger="plugins.platforms.telegram.telethon_adapter"):
        result = await adapter.send("77", "hello", metadata={"thread_id": "other-77"})
    assert result.success is False
    assert "ChannelPrivateError" in (result.error or "")
    assert [r for r in caplog.records if r.levelno == logging.WARNING]
