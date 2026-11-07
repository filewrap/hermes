from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["send", "edit_message"])
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "Before. <|tool_call:start|>execute_code "
            "<|tool_arg:start|>{\"code\":\"do not show\"} "
            "<|tool_arg:end|><|tool_call:end|> After.",
            "Before.  After.",
        ),
        (
            "Example:\n```python\n"
            "print('<|tool_call:start|>literal example')\n"
            "```\nTail.",
            "Example:\n```python\n"
            "print('<|tool_call:start|>literal example')\n"
            "```\nTail.",
        ),
    ],
)
async def test_mtproto_text_egress_filters_raw_wrappers_and_preserves_code(
    monkeypatch, method, content, expected,
):
    import plugins.platforms.telegram.mtproto_rich as rich

    captured = []
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = object()
    adapter._input_peer_for = AsyncMock(return_value=object())

    async def capture_request(request):
        captured.append(request)
        return SimpleNamespace(id=55)

    adapter._request_with_bounded_retry = AsyncMock(side_effect=capture_request)
    adapter._sent_message_id = MagicMock(return_value="55")
    monkeypatch.setattr(rich, "split_rich_message", lambda text, _limit: [text])
    monkeypatch.setattr(
        rich, "send_request",
        lambda _peer, text, **_kwargs: SimpleNamespace(message=text, random_id=1),
    )
    monkeypatch.setattr(
        rich, "edit_request",
        lambda _peer, _message_id, text, **_kwargs: SimpleNamespace(message=text),
    )

    if method == "send":
        result = await adapter.send("42", content)
    else:
        result = await adapter.edit_message("42", "55", content)

    assert result.success
    assert len(captured) == 1
    assert captured[0].message == expected
    assert "execute_code" not in captured[0].message
    assert "do not show" not in captured[0].message


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["send", "edit_message"])
async def test_mtproto_egress_rejects_content_that_is_only_a_raw_tool_block(method):
    adapter = TelethonAdapter(PlatformConfig())
    adapter._client = object()
    adapter._input_peer_for = AsyncMock(return_value=object())
    adapter._request_with_bounded_retry = AsyncMock()

    if method == "send":
        result = await adapter.send("42", "<|tool_call:start|>execute_code payload<|tool_call:end|>")
    else:
        result = await adapter.edit_message(
            "42", "55", "<|tool_call:start|>execute_code payload<|tool_call:end|>"
        )

    assert not result.success
    assert "empty" in result.error.lower()
    adapter._request_with_bounded_retry.assert_not_awaited()
