"""Standalone Telegram MEDIA:<path> caption delivery via native MTProto."""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest


def _install_mtproto_mock(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import telethon
    import plugins.platforms.telegram.telethon_adapter as adapter_module

    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.handlers = []
            self.commands = []
            self.send_id = 0

        async def start(self, *, bot_token):
            calls.append(("start", bot_token))

        async def get_me(self):
            return SimpleNamespace(bot=True, id=42, username="testbot")

        def is_connected(self):
            return False

        def add_event_handler(self, handler, event):
            self.handlers.append((handler, event))

        async def __call__(self, request):
            calls.append(("request", request))
            name = type(request).__name__
            if name == "SetBotCommandsRequest":
                self.commands = request.commands
                return True
            if name == "GetBotCommandsRequest":
                calls.append(("menu_response", [(row.command, row.description) for row in self.commands]))
                return self.commands
            self.send_id += 1
            return SimpleNamespace(id=self.send_id)

        async def send_file(self, chat_id, path, **kwargs):
            calls.append(("file", chat_id, path, kwargs))
            self.send_id += 1
            return SimpleNamespace(id=self.send_id)

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(telethon, "TelegramClient", FakeClient)
    monkeypatch.setattr(adapter_module, "_secret", lambda key, default="": {
        "TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "hash"}.get(key, default))
    monkeypatch.setattr(
        adapter_module.TelethonAdapter, "_session_file",
        lambda self: tmp_path / "profile" / "telethon.session",
    )
    return calls


def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TELEGRAM_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY",
        "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None, raising=False)
    monkeypatch.setattr(
        "gateway.platforms.base._detect_macos_system_proxy", lambda: None
    )


def _tmpfile(suffix: str) -> str:
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.write(b"x")
    f.close()
    return f.name


def _assert_menu_verified(calls):
    menu_requests = [call[1] for call in calls if len(call) > 1 and call[0] == "request" and
                     type(call[1]).__name__ in {"SetBotCommandsRequest", "GetBotCommandsRequest"}]
    assert [type(request).__name__ for request in menu_requests] == [
        "SetBotCommandsRequest", "GetBotCommandsRequest",
    ]
    assert next(call[1] for call in calls if call[0] == "menu_response") == [
        (row.command, row.description) for row in menu_requests[0].commands
    ]


def test_image_caption_rides_bubble_no_separate_text(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from tools.send_message_tool import _send_telegram

    _no_proxy(monkeypatch)
    calls = _install_mtproto_mock(monkeypatch, tmp_path)
    img = _tmpfile(".png")
    try:
        res = asyncio.run(
            _send_telegram("tok", "123", "This Caption", media_files=[(img, False)])
        )
        assert res["success"] is True
        _assert_menu_verified(calls)
        assert not any(type(call[1]).__name__ == "SendMessageRequest"
                       for call in calls if len(call) > 1 and call[0] == "request")
        media_calls = [call for call in calls if call[0] == "file"]
        assert len(media_calls) == 1
        assert media_calls[0][2] == img
        assert media_calls[0][3]["caption"] == "This Caption"
    finally:
        os.unlink(img)


def test_multi_file_keeps_separate_text(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from tools.send_message_tool import _send_telegram

    _no_proxy(monkeypatch)
    calls = _install_mtproto_mock(monkeypatch, tmp_path)
    img = _tmpfile(".png")
    img2 = _tmpfile(".jpg")
    try:
        res = asyncio.run(
            _send_telegram("tok", "123", "two pics", media_files=[(img, False), (img2, False)])
        )
        assert res["success"] is True
        _assert_menu_verified(calls)
        text_requests = [call[1] for call in calls if len(call) > 1 and call[0] == "request" and
                         type(call[1]).__name__ == "SendMessageRequest"]
        assert len(text_requests) == 1
        assert "two pics" in text_requests[0].message
        media_calls = [call for call in calls if call[0] == "file"]
        assert [call[2] for call in media_calls] == [img, img2]
        assert all("caption" not in call[3] for call in media_calls)
    finally:
        os.unlink(img)
        os.unlink(img2)


@pytest.mark.asyncio
async def test_live_adapter_delivers_every_media_path_with_optional_caption() -> None:
    """The gateway path sends every MEDIA file; ambiguous multi-file text stays separate."""
    from tools.send_message_tool import _send_live_adapter_media

    img = _tmpfile(".png")
    img2 = _tmpfile(".jpg")

    class Adapter:
        def __init__(self):
            self.text = []
            self.media = []

        async def send(self, *, chat_id, content, metadata=None):
            self.text.append((chat_id, content))
            return SimpleNamespace(success=True, message_id="text-1")

        async def send_image_file(self, chat_id, path, *, caption=None, reply_to=None, metadata=None):
            self.media.append((chat_id, path, caption))
            return SimpleNamespace(success=True, message_id=f"media-{len(self.media)}")

    adapter = Adapter()
    try:
        result = await _send_live_adapter_media(
            adapter, "123", "Two charts", [(img, False), (img2, False)],
            platform_name="telegram",
        )
        assert result["success"] is True
        assert adapter.text == [("123", "Two charts")]
        assert adapter.media == [("123", img, None), ("123", img2, None)]

        # For a single captionable file, short accompanying text is an optional native caption.
        adapter.media.clear()
        adapter.text.clear()
        result = await _send_live_adapter_media(
            adapter, "123", "One chart", [(img, False)], platform_name="telegram",
        )
        assert result["success"] is True
        assert adapter.text == []
        assert adapter.media == [("123", img, "One chart")]
    finally:
        os.unlink(img)
        os.unlink(img2)
