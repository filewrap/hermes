"""Telegram Rich Message RPC failures must stay failures; never downgrade to Bot API text."""
import sys
from types import ModuleType

import pytest
from telethon import types
from telethon.errors import BadRequestError

from gateway.config import PlatformConfig
from plugins.platforms.telegram.mtproto_rich import edit_request, send_request
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class RejectingClient:
    def __init__(self, error_type=BadRequestError):
        self.requests = []
        self.error_type = error_type

    async def __call__(self, request):
        self.requests.append(request)
        if self.error_type is BadRequestError:
            raise BadRequestError(request, "native rich payload rejected", code=400)
        raise self.error_type("network unavailable")


def _forbid_bot_api(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Bot API must never be used for Rich Message delivery")
    telegram_stub = ModuleType("telegram")
    telegram_stub.Bot = unexpected
    monkeypatch.setitem(sys.modules, "telegram", telegram_stub)


@pytest.mark.asyncio
async def test_bad_request_propagates_from_native_mtproto_dispatch(monkeypatch):
    _forbid_bot_api(monkeypatch)
    adapter = TelethonAdapter(PlatformConfig())
    client = RejectingClient()
    adapter._client = client
    request = send_request(types.PeerUser(42), "# Status\n\n| Step | State |\n|---|---|\n| Search | done |")

    with pytest.raises(BadRequestError):
        await adapter._mtproto_rich_request(request)

    assert client.requests == [request]
    assert request.rich_message.markdown.startswith("# Status")


@pytest.mark.asyncio
async def test_send_returns_mtproto_failure_without_retry_or_text_downgrade(monkeypatch):
    _forbid_bot_api(monkeypatch)
    adapter = TelethonAdapter(PlatformConfig())
    client = RejectingClient()
    adapter._client = client

    result = await adapter.send("42", "## Results\n\n| Case | Status |\n|---|---|\n| rich | rejected |")

    assert result.success is False
    assert result.error == "Telegram MTProto send failed: BadRequestError"
    assert len(client.requests) == 1
    assert type(client.requests[0]).__name__ == "SendMessageRequest"
    assert client.requests[0].rich_message.markdown.startswith("## Results")


@pytest.mark.asyncio
async def test_retry_wrapper_keeps_telegram_formatting_failure_native(monkeypatch):
    _forbid_bot_api(monkeypatch)
    adapter = TelethonAdapter(PlatformConfig())
    client = RejectingClient()
    adapter._client = client

    result = await adapter._send_with_retry("42", "## Results\n\n**native only**")

    assert result.success is False
    assert result.error == "Telegram MTProto send failed: BadRequestError"
    assert len(client.requests) == 1
    assert isinstance(client.requests[0].rich_message, types.InputRichMessageMarkdown)


@pytest.mark.asyncio
async def test_edit_returns_mtproto_failure_without_bot_api_retry(monkeypatch):
    _forbid_bot_api(monkeypatch)
    adapter = TelethonAdapter(PlatformConfig())
    client = RejectingClient()
    adapter._client = client

    result = await adapter.edit_message("42", "9", "# Finished")

    assert result.success is False
    assert result.error == "Telegram MTProto edit failed: BadRequestError"
    assert len(client.requests) == 1
    assert type(client.requests[0]).__name__ == "EditMessageRequest"
    assert client.requests[0].rich_message.markdown == "# Finished"


def test_send_and_edit_requests_serialize_native_rich_fields():
    markdown = "## Native\n\n**formatted**"
    sent = send_request(types.PeerUser(42), markdown)
    edited = edit_request(types.PeerUser(42), 9, markdown)

    assert type(sent.rich_message).__name__ == "InputRichMessageMarkdown"
    assert type(edited.rich_message).__name__ == "InputRichMessageMarkdown"
    assert sent.rich_message.markdown == edited.rich_message.markdown
    assert sent._bytes()
    assert edited._bytes()
