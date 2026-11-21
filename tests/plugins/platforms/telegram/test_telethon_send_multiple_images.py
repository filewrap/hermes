"""``send_multiple_images`` routes GIFs through ``send_animations``.

A GIF cannot join a media group and must go out as an animation (``send_animation``),
never down the photo path — a GIF sent as a photo loses its animation. Stills keep
their existing album / per-photo routing.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _adapter():
    return SimpleNamespace(
        name="telegram",
        send_animation=AsyncMock(return_value=SendResult(success=True, message_id="7")),
        send_image_file=AsyncMock(return_value=SendResult(success=True, message_id="8")),
        send_image=AsyncMock(return_value=SendResult(success=True, message_id="9")),
    )


@pytest.mark.asyncio
async def test_gif_and_still_take_their_own_paths():
    adapter = _adapter()
    result = await TelethonAdapter.send_multiple_images(
        adapter, "4242",
        [("file:///tmp/a.gif", "funny"), ("file:///tmp/b.jpg", "still")],
    )
    assert result.success is True
    adapter.send_animation.assert_awaited_once()
    assert adapter.send_animation.await_args.kwargs["animation_url"] == "file:///tmp/a.gif"
    adapter.send_image_file.assert_awaited_once()
    assert adapter.send_image_file.await_args.args[1] == "/tmp/b.jpg"
    adapter.send_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_gif_goes_to_send_animation_not_send_image():
    adapter = _adapter()
    result = await TelethonAdapter.send_multiple_images(
        adapter, "4242", [("https://example.invalid/a.gif?x=1", "remote")])
    assert result.success is True
    adapter.send_animation.assert_awaited_once()
    adapter.send_image.assert_not_awaited()
    adapter.send_image_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_gif_batch_still_delivers():
    adapter = _adapter()
    result = await TelethonAdapter.send_multiple_images(
        adapter, "4242",
        [("file:///tmp/a.gif", "one"), ("file:///tmp/b.gif", "two")],
    )
    assert result.success is True
    assert adapter.send_animation.await_count == 2
    adapter.send_image_file.assert_not_awaited()
