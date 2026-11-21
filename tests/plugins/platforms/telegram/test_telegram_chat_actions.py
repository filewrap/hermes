"""Telegram chat actions: the indicator must match what the bot is doing.

Typing state expires after ~5s, so the refresh loop re-sends the current
action; a file upload shows its upload action (photo / document / video /
voice) instead of typing, and a forum topic gets ``top_msg_id`` so the
indicator shows inside the topic rather than only at chat level.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _FakeClient:
    """Records SetTypingRequest calls and pretends file sends succeed."""

    def __init__(self):
        self.typing_requests: list = []
        self.sent_files: list = []

    async def __call__(self, request):
        self.typing_requests.append(request)

    async def send_file(self, peer, file_path, **kwargs):
        self.sent_files.append((peer, file_path, kwargs))
        return SimpleNamespace(id=42)


def _adapter(tmp_path):
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = _FakeClient()
    adapter._peer_for = lambda chat_id: ("peer", str(chat_id))
    return adapter


def _action_name(request):
    return type(request.action).__name__


@pytest.mark.asyncio
async def test_send_typing_defaults_to_typing_action(tmp_path):
    adapter = _adapter(tmp_path)

    await adapter.send_typing("100")

    assert len(adapter._client.typing_requests) == 1
    assert _action_name(adapter._client.typing_requests[0]) == "SendMessageTypingAction"
    assert adapter._client.typing_requests[0].top_msg_id is None


@pytest.mark.asyncio
async def test_send_typing_carries_topic_id_into_forum_topic(tmp_path):
    adapter = _adapter(tmp_path)

    await adapter.send_typing("100", metadata={"thread_id": "7"})

    assert adapter._client.typing_requests[0].top_msg_id == 7


@pytest.mark.asyncio
async def test_pinned_upload_action_is_resent_until_cleared(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.set_chat_action("100", "upload_document")

    await adapter.send_typing("100")
    assert _action_name(adapter._client.typing_requests[0]) == "SendMessageUploadDocumentAction"

    adapter.clear_chat_action("100")
    await adapter.send_typing("100")
    assert _action_name(adapter._client.typing_requests[1]) == "SendMessageTypingAction"


@pytest.mark.asyncio
async def test_metadata_action_overrides_and_unknown_falls_back_to_typing(tmp_path):
    adapter = _adapter(tmp_path)

    await adapter.send_typing("100", metadata={"telegram_chat_action": "upload_photo"})
    assert _action_name(adapter._client.typing_requests[0]) == "SendMessageUploadPhotoAction"

    await adapter.send_typing("100", metadata={"telegram_chat_action": "nope"})
    assert _action_name(adapter._client.typing_requests[1]) == "SendMessageTypingAction"


@pytest.mark.asyncio
async def test_stop_typing_cancels_in_the_same_topic(tmp_path):
    adapter = _adapter(tmp_path)

    await adapter.stop_typing("100", metadata={"thread_id": "7"})

    request = adapter._client.typing_requests[0]
    assert _action_name(request) == "SendMessageCancelAction"
    assert request.top_msg_id == 7


@pytest.mark.asyncio
async def test_send_file_shows_upload_action_and_clears_it(tmp_path):
    adapter = _adapter(tmp_path)
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)

    result = await adapter.send_file("100", str(image), metadata={})

    assert result.success is True
    assert len(adapter._client.sent_files) == 1
    assert "100" not in adapter._chat_action, "the upload action must not leak past the send"
    upload = adapter._client.typing_requests[0]
    assert _action_name(upload) == "SendMessageUploadPhotoAction"


@pytest.mark.parametrize(
    "name,kwargs,expected",
    [
        ("a.mp4", {}, "upload_video"),
        ("a.ogg", {"voice_note": True}, "upload_audio"),
        ("a.mp4", {"video_note": True}, "upload_round"),
        ("a.pdf", {"force_document": True}, "upload_document"),
        ("a.pdf", {}, "upload_document"),
        ("a.mp3", {}, "upload_audio"),
    ],
)
def test_upload_action_for_file(name, kwargs, expected):
    assert TelethonAdapter._upload_action_for_file(name, **kwargs) == expected


def test_chat_action_names_resolve_to_real_telethon_types():
    for name in TelethonAdapter._CHAT_ACTION_TYPE_NAMES:
        action = TelethonAdapter._build_chat_action(name)
        assert type(action).__name__ == TelethonAdapter._CHAT_ACTION_TYPE_NAMES[name]
    assert type(TelethonAdapter._build_chat_action("bogus")).__name__ == "SendMessageTypingAction"
    assert TelethonAdapter._build_chat_action("upload_document").progress == 0
