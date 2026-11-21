"""Real adapter routing for bounded albums and deferred native downloads."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageType
from gateway.session import SessionSource
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def adapter_for(monkeypatch, tmp_path, *, authorized=True):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="7", chat_type="dm",
                           user_id="7", user_name="Alice")
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = AsyncMock()
    adapter._source = AsyncMock(return_value=source)
    adapter._multiuser_home = lambda _source: None
    adapter._authorized = lambda _source: authorized
    adapter._group_triggered = AsyncMock(return_value=True)
    adapter._refresh_command_menu = AsyncMock()
    adapter._cache_inbound_peer = AsyncMock()
    adapter._send_rich_ui = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._remember_peer("7", types.InputPeerUser(7, 0))
    return adapter, source


def document(index, caption=""):
    return SimpleNamespace(
        id=100 + index, grouped_id=55, message=caption, photo=None,
        document=object(), file=SimpleNamespace(name=f"part-{index}.zip",
            size=300 * 1024 * 1024, mime_type="application/zip"),
        video=None, audio=None, voice=None, sticker=None, reply_to=None,
        reply_to_msg_id=None, fwd_from=None,
    )


@pytest.mark.asyncio
async def test_ten_document_album_is_described_once_before_any_large_download(monkeypatch, tmp_path):
    adapter, _source = adapter_for(monkeypatch, tmp_path)
    messages = [document(i, "First caption" if i == 0 else "Last caption" if i == 9 else "")
                for i in range(10)]
    album = SimpleNamespace(messages=messages, message=messages[0], chat_id=7)
    # Each native NewMessage also fires, but Album is the only admitted copy.
    await adapter._on_new_message(SimpleNamespace(message=messages[0]))
    adapter.handle_message.assert_not_awaited()
    await adapter._on_album(album)
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.message_type == MessageType.DOCUMENT
    assert "First caption" in event.text and "Last caption" in event.text
    assert all(f"part-{i}.zip" in event.text for i in range(10))
    assert "application/zip" in event.text and "download deferred" in event.text
    assert event.media_urls == []
    adapter._client.download_media.assert_not_awaited()
    adapter._send_rich_ui.assert_awaited_once()
    assert len(adapter._attachment_ui()._pending) == 10
    assert adapter._client.await_count == 10  # Native acknowledgement for each attachment.
    assert all(call.args[0].reaction[0].emoticon == "🆒"
               for call in adapter._client.await_args_list)


@pytest.mark.asyncio
async def test_unauthorized_album_never_downloads_reacts_or_creates_controls(monkeypatch, tmp_path):
    adapter, _source = adapter_for(monkeypatch, tmp_path, authorized=False)
    await adapter._on_album(SimpleNamespace(messages=[document(0)], chat_id=7))
    adapter.handle_message.assert_not_awaited()
    adapter._client.assert_not_awaited()
    adapter._client.download_media.assert_not_awaited()
    adapter._send_rich_ui.assert_not_awaited()
    adapter._refresh_command_menu.assert_not_awaited()


@pytest.mark.asyncio
async def test_deferred_download_callback_dispatches_the_downloaded_file(monkeypatch, tmp_path):
    adapter, source = adapter_for(monkeypatch, tmp_path)
    monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda _home: False)
    path = tmp_path / "downloaded.zip"
    path.write_bytes(b"file")
    message = document(0)
    controller = adapter._attachment_ui()
    controller._pending["test-token"] = {"message": message}
    controller.handle_callback = AsyncMock(return_value={
        "media": SimpleNamespace(path=path, media_type="document"),
        "note": "Downloaded the requested archive.",
    })
    adapter._dispatch_attachment_event = AsyncMock()
    callback = SimpleNamespace(data=b"tdl:test-token", message_id=99, answer=AsyncMock())
    await adapter._on_callback_query(callback)
    adapter._dispatch_attachment_event.assert_awaited_once_with(
        source, message, [str(path)], ["document"], "Downloaded the requested archive.")


@pytest.mark.asyncio
async def test_receive_limit_does_not_authorize_oversize_outbound_upload(monkeypatch, tmp_path):
    adapter, _source = adapter_for(monkeypatch, tmp_path)
    path = tmp_path / "oversize.bin"
    with path.open("wb") as handle:
        handle.truncate(adapter.MAX_UPLOAD_BYTES + 1)
    result = await adapter.send_document("7", str(path), caption="An optional caption")
    assert result.success is False
    assert "2 GB" in result.error and "receive setting" in result.error
    adapter._client.send_file.assert_not_awaited()
    adapter._client.upload_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_menu_readback_mismatch_is_not_cached_and_can_be_retried(monkeypatch, tmp_path):
    adapter, source = adapter_for(monkeypatch, tmp_path)
    adapter._help_skill_commands = lambda _source: {}
    result = SimpleNamespace(verified=False, scope="private", registered_count=80, overflow_count=0)
    register = AsyncMock(return_value=result)
    monkeypatch.setattr("plugins.platforms.telegram.command_menu.register_personal_command_menu", register)
    assert await TelethonAdapter._refresh_command_menu(adapter, source) is None
    assert adapter._command_menu_hashes == {}
    result.verified = True
    assert await TelethonAdapter._refresh_command_menu(adapter, source) is result
    assert register.await_count == 2
    assert len(adapter._command_menu_hashes) == 1
