"""MTProto uploads must survive caching and reach the gateway as readable files."""

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageType
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _zip_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("avisha-main/README.md", "# Uploaded project\n")
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("forwarded,caption", [(False, ""), (True, "Here it is.")])
async def test_zip_upload_reaches_gateway_with_readable_cache_path(monkeypatch, tmp_path, forwarded, caption):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    payload = _zip_bytes()
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = SimpleNamespace(download_media=AsyncMock(return_value=payload))
    monkeypatch.setattr(adapter, "_authorized", lambda source: True)
    adapter.handle_message = AsyncMock()
    sender = SimpleNamespace(id=7, first_name="Alice", bot=False)
    message = SimpleNamespace(
        id=12, message=caption, document=object(),
        photo=None, video=None, voice=None, audio=None, sticker=None,
        file=SimpleNamespace(name="avisha-main.zip", size=len(payload), mime_type="application/zip"),
        fwd_from=SimpleNamespace(from_id=8) if forwarded else None,
        reply_to=None, reply_to_msg_id=None,
    )

    await adapter._on_new_message(SimpleNamespace(message=message, chat_id=7, sender=sender, chat=sender))

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.message_type == MessageType.DOCUMENT
    if caption:
        assert event.text.startswith(caption)
    assert "Telegram attachment 1" in event.text
    assert "avisha-main.zip" in event.text
    assert "application/zip" in event.text
    assert event.media_types == ["application/zip"]
    assert len(event.media_urls) == 1
    cached_path = Path(event.media_urls[0])
    assert cached_path.is_relative_to(home / "cache" / "documents")
    assert cached_path.name.endswith("avisha-main.zip")
    assert cached_path.read_bytes() == payload
    with zipfile.ZipFile(cached_path) as archive:
        assert archive.read("avisha-main/README.md") == b"# Uploaded project\n"
    from gateway.run_inbound import GatewayInboundMixin

    context = GatewayInboundMixin._prepend_inbound_document_notes(event, event.text)
    assert str(cached_path) in context
    assert "avisha-main.zip" in context
    assert "application/zip" in context
    if caption:
        assert caption in context
    adapter._client.download_media.assert_awaited_once_with(message, file=bytes)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,mime,payload,expected_mime", [
    ("notes.txt", "application/octet-stream", b"notes", "text/plain"),
    ("photo.png", "image/png", b"\x89PNG\r\n\x1a\nminimal", "image/png"),
    ("voice.ogg", "audio/ogg", b"audio", "audio/ogg"),
    ("clip.mp4", "video/mp4", b"video", "video/mp4"),
    ("unknown.custom", "", b"unknown", "application/octet-stream"),
])
async def test_cached_media_returns_normalized_type_and_preserves_bytes(
    monkeypatch, tmp_path, name, mime, payload, expected_mime,
):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = SimpleNamespace(download_media=AsyncMock(return_value=payload))
    message = SimpleNamespace(
        photo=None, document=object(),
        file=SimpleNamespace(name=name, mime_type=mime, size=len(payload)),
    )

    paths, media_types, note = await adapter._cache_inbound_media(message)

    assert note == ""
    assert media_types == [expected_mime]
    assert len(paths) == 1
    assert Path(paths[0]).is_relative_to(home / "cache")
    assert Path(paths[0]).read_bytes() == payload
