"""Telethon port of upstream's album contract: still photos go out as one
grouped media message (10 per chunk); a chunk whose reply was lost is
reported ambiguous and never resent; any other refusal falls back
photo by photo."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions
from telethon.errors.rpcerrorlist import FloodWaitError, ServerError

from gateway.platforms.base import SendResult
from plugins.platforms.telegram import telegram_album as alb


def _photos(tmp_path, count=2):
    paths = []
    for i in range(count):
        path = tmp_path / f"photo{i}.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0fake")
        paths.append((f"file://{path}", f"alt {i}"))
    return paths


def _adapter(*, chunk_error=None):
    client = SimpleNamespace(
        _file_to_media=AsyncMock(return_value=(None, SimpleNamespace(), None))
    )
    retry = AsyncMock(return_value=None)
    if chunk_error is not None:
        retry = AsyncMock(side_effect=chunk_error)
    return SimpleNamespace(
        name="telegram",
        _client=client,
        _input_peer_for=AsyncMock(return_value="peer"),
        _reply_top_target=lambda _m: None,
        _reply_target=lambda _r, _m: None,
        _request_with_bounded_retry=retry,
        send_image_file=AsyncMock(
            return_value=SendResult(success=True, message_id="9")
        ),
        send_image=AsyncMock(return_value=SendResult(success=True, message_id="9")),
    )


def test_reply_lost_timeout_and_server_error_but_not_flood_wait():
    assert alb.reply_lost(TimeoutError()) is True
    assert (
        alb.reply_lost(ServerError(request=None, message="Internal server error"))
        is True
    )
    assert alb.reply_lost(FloodWaitError(request=None, capture=5)) is False
    assert alb.reply_lost(RuntimeError("refused")) is False


def test_caption_truncates_at_1024_utf16_units():
    assert alb._caption_1024("short") == "short"
    assert len(alb._caption_1024("x" * 2000)) == 1024
    assert alb._caption_1024("\U0001f600" * 600) == "\U0001f600" * 512


@pytest.mark.asyncio
async def test_album_sends_one_multi_media_request(tmp_path):
    adapter = _adapter()
    result = await alb.send_photo_album(adapter, "4242", _photos(tmp_path, 2))
    assert result.success is True
    assert result.raw_response is None
    request = adapter._request_with_bounded_retry.await_args.args[0]
    assert isinstance(request, functions.messages.SendMultiMediaRequest)
    assert len(request.multi_media) == 2
    assert [single.message for single in request.multi_media] == ["alt 0", "alt 1"]
    adapter.send_image_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_album_refusal_falls_back_photo_by_photo(tmp_path):
    adapter = _adapter(chunk_error=RuntimeError("refused"))
    result = await alb.send_photo_album(adapter, "4242", _photos(tmp_path, 2))
    assert result.success is True
    assert adapter.send_image_file.await_count == 2


@pytest.mark.asyncio
async def test_album_reply_lost_is_ambiguous_and_never_resent(tmp_path):
    adapter = _adapter(chunk_error=TimeoutError())
    result = await alb.send_photo_album(adapter, "4242", _photos(tmp_path, 2))
    assert result.success is True
    assert result.raw_response["ambiguous"] is True
    adapter.send_image_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_album_refusal_without_fallback_hands_back_refused(tmp_path):
    adapter = _adapter(chunk_error=RuntimeError("refused"))
    result = await alb.send_photo_album(
        adapter, "4242", _photos(tmp_path, 2), per_photo_fallback=False
    )
    assert result.success is False
    assert len(result.raw_response["refused"]) == 2
    adapter.send_image_file.assert_not_awaited()
