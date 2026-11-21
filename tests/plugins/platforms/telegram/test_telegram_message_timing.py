from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock

import pytest
from telethon import types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.mark.asyncio
async def test_send_logs_receipt_to_mtproto_completion_without_payload(caplog):
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = AsyncMock()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerChannel(12, 456))
    adapter._mtproto_rich_request = AsyncMock(return_value=type("Sent", (), {"id": 901})())
    adapter._telegram_pending_receipts[("-10012", "77")] = time.monotonic() - 0.25
    caplog.set_level(logging.INFO, logger="plugins.platforms.telegram.telethon_adapter")
    payload = "PRIVATE_PAYLOAD_NEVER_LOG"

    result = await adapter.send(chat_id="-10012", content=payload, reply_to="77")

    assert result.success is True
    assert "Telegram inbound-to-MTProto response completed" in caplog.text
    assert "chat_id=-10012" in caplog.text
    assert "inbound_message_id=77" in caplog.text
    assert "elapsed_ms=" in caplog.text
    assert payload not in caplog.text
    assert not adapter._telegram_pending_receipts
