"""Regression tests for Telegram's native MTProto proxy and standalone sender."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest


def test_native_proxy_url_is_translated_to_telethon_settings(monkeypatch):
    from plugins.platforms.telegram import telethon_adapter

    monkeypatch.setattr(
        telethon_adapter,
        "_secret",
        lambda name, default="": "socks5://user:pass@127.0.0.1:1080" if name == "TELEGRAM_PROXY" else default,
    )
    assert telethon_adapter._proxy_settings() == {
        "proxy_type": "socks5",
        "addr": "127.0.0.1",
        "port": 1080,
        "rdns": True,
        "username": "user",
        "password": "pass",
    }


def test_invalid_proxy_scheme_fails_closed(monkeypatch):
    from plugins.platforms.telegram import telethon_adapter

    monkeypatch.setattr(telethon_adapter, "_secret", lambda name, default="": "ftp://proxy.example:21")
    with pytest.raises(ValueError, match="TELEGRAM_PROXY"):
        telethon_adapter._proxy_settings()


def test_send_message_standalone_delegates_to_native_mtproto(monkeypatch):
    from plugins.platforms.telegram import telethon_adapter
    from tools.send_message_senders import _send_telegram

    send = AsyncMock(return_value={"success": True, "platform": "telegram", "message_id": "42"})
    monkeypatch.setattr(telethon_adapter, "send_standalone", send)
    result = asyncio.run(
        _send_telegram(
            "bot-token", "-100123", "hello", media_files=[("/tmp/file.ogg", True)],
            thread_id="77", disable_link_previews=True, force_document=True,
        )
    )

    assert result["success"] is True
    send.assert_awaited_once_with(
        "bot-token", "-100123", "hello", media_files=[("/tmp/file.ogg", True)],
        thread_id="77", disable_link_previews=True, force_document=True,
    )
