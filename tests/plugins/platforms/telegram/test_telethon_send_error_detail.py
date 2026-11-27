"""A failed Telegram MTProto send must keep the server's real reason.

``TelethonAdapter.send``/``edit_message`` used to report only the exception type
(``Telegram MTProto send failed: BadRequestError``), discarding the RPC error
text the server sent — so a rejected rich payload was undiagnosable from the
logs. The ``error`` string itself stays exactly as the pinned
``test_telethon_rich_fallback.py`` asserts it; the reason travels in
``raw_response["error_detail"]`` and on the warning log line.
"""

import logging

import pytest
from telethon import types
from telethon.errors import BadRequestError

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _RejectingClient:
    def __init__(self, message="RICH_MESSAGE_INVALID", code=400):
        self.requests = []
        self.message = message
        self.code = code

    async def __call__(self, request):
        self.requests.append(request)
        raise BadRequestError(request, self.message, code=self.code)


def _adapter():
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = _RejectingClient()
    return adapter


@pytest.mark.asyncio
async def test_send_keeps_rpc_reason_in_error_detail(caplog):
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        result = await adapter.send("42", "## Results\n\n`code` here")

    assert result.success is False
    # Pinned by test_telethon_rich_fallback.py: the error string does not change.
    assert result.error == "Telegram MTProto send failed: BadRequestError"
    assert isinstance(result.raw_response, dict)
    detail = result.raw_response.get("error_detail", "")
    assert "400" in detail and "RICH_MESSAGE_INVALID" in detail
    assert "RICH_MESSAGE_INVALID" in caplog.text


@pytest.mark.asyncio
async def test_edit_keeps_rpc_reason_in_error_detail(caplog):
    adapter = _adapter()
    with caplog.at_level(logging.WARNING):
        result = await adapter.edit_message("42", "7", "## Results\n\n`code` here")

    assert result.success is False
    assert result.error == "Telegram MTProto edit failed: BadRequestError"
    assert isinstance(result.raw_response, dict)
    detail = result.raw_response.get("error_detail", "")
    assert "400" in detail and "RICH_MESSAGE_INVALID" in detail
    assert "RICH_MESSAGE_INVALID" in caplog.text


@pytest.mark.asyncio
async def test_send_error_detail_never_carries_payload_text(caplog):
    """The detail is the server's reason, not the message that was sent."""
    adapter = _adapter()
    secret = "sk-live-very-secret-value-12345"
    with caplog.at_level(logging.WARNING):
        result = await adapter.send("42", f"key is `{secret}`")

    assert result.success is False
    detail = (result.raw_response or {}).get("error_detail", "")
    assert secret not in detail
    assert secret not in caplog.text
