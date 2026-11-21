"""Streamed responses carry the threaded-reply marker.

Every send the stream consumer makes is a turn response message, so
``_metadata_for_send`` marks them all; the Telegram adapter chains marked
sends to each other's messages when the threaded-replies beta is on.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer


def test_metadata_for_send_marks_response_sends_as_threaded():
    """Interim and final sends alike carry the threaded-reply marker."""
    consumer = GatewayStreamConsumer(MagicMock(), "chat_123")
    for final in (False, True):
        meta = consumer._metadata_for_send(final=final)
        assert meta is not None
        assert meta.get("telegram_threaded_reply") is True


@pytest.mark.asyncio
async def test_first_send_carries_the_threaded_marker():
    """The first streamed send hands the marker to the adapter."""
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="msg_1"))
    adapter.MAX_MESSAGE_LENGTH = 4096

    consumer = GatewayStreamConsumer(adapter, "chat_123")
    await consumer._send_or_edit("Hello there.")

    adapter.send.assert_called_once()
    metadata = adapter.send.call_args[1].get("metadata") or {}
    assert metadata.get("telegram_threaded_reply") is True
