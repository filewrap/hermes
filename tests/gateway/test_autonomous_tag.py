"""Behaviour contracts for the per-message green autonomous tag.

The tag rides atop every message of the mode while it is active with
badges on: finals through ``send_final_ledgered`` (metadata flag, first
part only) and live drafts through ``ResponseStreamDraft``. Markdown
finals become native blocks with the row at index 0 — they are never
flipped to HTML, which would render the Markdown literally. Explicit
Markdown and text frames cannot carry a native row and stay untagged
rather than changing format; the sync draft stays the plain final chunk
so the client adopts the final. Groups, guest threads, and other
platforms get nothing.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import types

from gateway import autonomous_state
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.streaming_response import ResponseStreamDraft
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from tests.waiting import async_wait_until

OWNER_ID = "8495104954"


@pytest.fixture
def auto_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


def _dm_source(*, chat_type="dm", thread_id=None, platform=Platform.TELEGRAM):
    return SimpleNamespace(platform=platform, chat_id=OWNER_ID,
                           chat_type=chat_type, thread_id=thread_id)


def _plain_text(node):
    """Innermost plain string of a RichText node."""
    text = getattr(node, "text", None)
    while text is not None and not isinstance(text, str):
        text = getattr(text, "text", None)
    return text or ""


def _is_tag_row(block):
    """True when *block* is the green autonomous tag row."""
    if type(block).__name__ != "PageBlockButtonRow":
        return False
    buttons = getattr(block, "buttons", None) or []
    if len(buttons) != 1:
        return False
    button = buttons[0]
    return (_plain_text(button.text) == "autonomous"
            and type(button.type).__name__ == "InlineButtonTypeDisabled"
            and bool(getattr(button.style, "bg_success", False)))


def _blocks_text(blocks):
    """Plain text of rich blocks, skipping Thinking blocks and tag rows."""
    out = []

    def _walk(node):
        if node is None:
            return
        if type(node).__name__ in ("PageBlockThinking", "PageBlockButtonRow"):
            return
        text = getattr(node, "text", None)
        if isinstance(text, str) and text:
            out.append(text)
        for sub in getattr(node, "texts", []) or []:
            _walk(sub)
        for item in getattr(node, "items", []) or []:
            _walk(item)
        if text is not None and not isinstance(text, str):
            _walk(text)

    for block in blocks or []:
        _walk(block)
    return "".join(out)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def test_tag_gate_on_when_active_and_badges_default(auto_home):
    autonomous_state.set_active(auto_home, True)

    assert autonomous_state.tag_enabled_for_delivery(None, _dm_source()) is False
    assert autonomous_state.tag_enabled_for_delivery(SimpleNamespace(), _dm_source()) is True


def test_tag_gate_off_when_badges_disabled(auto_home):
    from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

    autonomous_state.set_active(auto_home, True)
    TelegramPersonalSettingsUI._set_autonomous_badges(auto_home, False)

    assert autonomous_state.autonomous_tag_enabled(SimpleNamespace()) is False
    assert autonomous_state.tag_enabled_for_delivery(SimpleNamespace(), _dm_source()) is False


def test_tag_gate_off_when_inactive(auto_home):
    assert autonomous_state.autonomous_tag_enabled(SimpleNamespace()) is False
    assert autonomous_state.tag_enabled_for_delivery(SimpleNamespace(), _dm_source()) is False


def test_tag_gate_off_in_group_guest_and_other_platform(auto_home):
    autonomous_state.set_active(auto_home, True)
    adapter = SimpleNamespace()

    assert autonomous_state.tag_enabled_for_delivery(
        adapter, _dm_source(chat_type="group")) is False
    assert autonomous_state.tag_enabled_for_delivery(
        adapter, _dm_source(thread_id="guest-123")) is False
    assert autonomous_state.tag_enabled_for_delivery(
        adapter, _dm_source(platform=Platform.DISCORD)) is False


def test_tag_metadata_adds_flag_without_mutating(auto_home):
    autonomous_state.set_active(auto_home, True)
    metadata = {"thread_id": "9"}

    tagged = autonomous_state.tag_metadata(SimpleNamespace(), _dm_source(), metadata)

    assert tagged == {"thread_id": "9", "telegram_autonomous_tag": True}
    assert metadata == {"thread_id": "9"}


def test_tag_metadata_returns_input_unchanged_when_gate_fails(auto_home):
    metadata = {"thread_id": "9"}

    assert autonomous_state.tag_metadata(
        SimpleNamespace(), _dm_source(), metadata) is metadata


def test_badges_adapter_extra_wins_over_config(auto_home):
    autonomous_state.set_active(auto_home, True)
    adapter = SimpleNamespace(config=SimpleNamespace(extra={"autonomous_badges": False}))

    assert autonomous_state.autonomous_tag_enabled(adapter) is False


# ---------------------------------------------------------------------------
# Rich payloads
# ---------------------------------------------------------------------------

def test_markdown_final_tagged_as_blocks_not_html():
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    rich = input_rich_message("## Hello\n\nSome **bold** text", autonomous_tag=True)

    assert type(rich).__name__ == "InputRichMessage"
    assert _is_tag_row(rich.blocks[0])
    assert "Hello" in _blocks_text(rich.blocks)
    assert "bold" in _blocks_text(rich.blocks)


def test_markdown_final_untagged_stays_markdown():
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    rich = input_rich_message("## Hello\n\nSome **bold** text")

    assert type(rich).__name__ == "InputRichMessageMarkdown"


def test_html_final_tag_prepended_as_text():
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    rich = input_rich_message("<h1>Hi</h1><p>body</p>", autonomous_tag=True)

    assert type(rich).__name__ == "InputRichMessageHTML"
    assert rich.html.startswith("<tg-button-row>")
    assert "autonomous" in rich.html
    assert "<h1>Hi</h1>" in rich.html


def test_explicit_markdown_stays_untagged():
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    rich = input_rich_message("## Hello", rich_format="markdown", autonomous_tag=True)

    assert type(rich).__name__ == "InputRichMessageMarkdown"


def test_math_final_tagged_with_row_first():
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    rich = input_rich_message("```latex\n\\sum_{n=1}^N n\n```", autonomous_tag=True)

    assert type(rich).__name__ == "InputRichMessage"
    assert _is_tag_row(rich.blocks[0])
    assert any(type(block).__name__ == "PageBlockMath" for block in rich.blocks)


def test_blocks_final_tagged_with_row_first():
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    rich = input_rich_message("## Hello", rich_format="blocks", autonomous_tag=True)

    assert type(rich).__name__ == "InputRichMessage"
    assert _is_tag_row(rich.blocks[0])
    assert "Hello" in _blocks_text(rich.blocks)


def test_send_request_tagged_and_untagged():
    from plugins.platforms.telegram.mtproto_rich import send_request

    tagged = send_request(types.PeerUser(1), "## Hello", autonomous_tag=True)
    assert type(tagged.rich_message).__name__ == "InputRichMessage"
    assert _is_tag_row(tagged.rich_message.blocks[0])
    assert "Hello" in tagged.message

    untagged = send_request(types.PeerUser(1), "## Hello")
    assert type(untagged.rich_message).__name__ == "InputRichMessageMarkdown"


def test_send_request_prebuilt_blocks_tagged():
    from plugins.platforms.telegram.mtproto_rich import input_rich_message, send_request

    built = input_rich_message("## Hello", rich_format="blocks")
    request = send_request(types.PeerUser(1), built, autonomous_tag=True)

    assert _is_tag_row(request.rich_message.blocks[0])
    # The companion message text comes only from Paragraph blocks, so a
    # title-only body reads as the blank fallback; the content survives
    # in the blocks themselves.
    assert "Hello" in _blocks_text(request.rich_message.blocks)


# ---------------------------------------------------------------------------
# Live drafts
# ---------------------------------------------------------------------------

class _DraftClient:
    """Fake Telethon client capturing SetTypingRequests."""

    def __init__(self):
        self.requests = []
        self.sent = []

    async def __call__(self, request):
        self.requests.append(request)
        self.sent.append(request)
        return SimpleNamespace()


def _fake_draft_adapter(client):
    adapter = SimpleNamespace(_client=client)
    adapter.config = PlatformConfig(extra={})
    adapter._truthy_setting = TelethonAdapter._truthy_setting
    adapter._peer_cache = {}

    async def _input_peer_for(chat_id):
        return types.InputPeerUser(int(chat_id), 123)

    adapter._input_peer_for = _input_peer_for
    return adapter


def _rich_draft_actions(client):
    actions = []
    for request in client.sent:
        if type(request).__name__ != "SetTypingRequest":
            continue
        action = request.action
        if type(action).__name__ == "InputSendMessageRichMessageDraftAction":
            actions.append(action)
    return actions


@pytest.mark.asyncio
async def test_thinking_draft_tagged_when_active(auto_home):
    autonomous_state.set_active(auto_home, True)
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_draft_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_reasoning("Thinking through it")
        await async_wait_until(lambda: len(_rich_draft_actions(client)) == 1)

        action = _rich_draft_actions(client)[0]
        assert _is_tag_row(action.rich_message.blocks[0])
        assert type(action.rich_message.blocks[1]).__name__ == "PageBlockThinking"
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_thinking_draft_untagged_when_inactive(auto_home):
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_draft_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_reasoning("Thinking through it")
        await async_wait_until(lambda: len(_rich_draft_actions(client)) == 1)

        action = _rich_draft_actions(client)[0]
        assert type(action.rich_message.blocks[0]).__name__ == "PageBlockThinking"
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_rich_draft_with_reasoning_tagged_when_active(auto_home):
    autonomous_state.set_active(auto_home, True)
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_draft_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_reasoning("Thinking through it")
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_rich_draft_actions(client)) == 2)

        action = _rich_draft_actions(client)[1]
        assert _is_tag_row(action.rich_message.blocks[0])
        assert type(action.rich_message.blocks[1]).__name__ == "PageBlockThinking"
        assert "Hello" in _blocks_text(action.rich_message.blocks)
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_rich_draft_content_only_tagged_as_blocks_when_active(auto_home):
    autonomous_state.set_active(auto_home, True)
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_draft_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_content("Hello **world**")
        await async_wait_until(lambda: len(_rich_draft_actions(client)) == 1)

        action = _rich_draft_actions(client)[0]
        assert type(action.rich_message).__name__ == "InputRichMessage"
        assert _is_tag_row(action.rich_message.blocks[0])
        assert "Hello" in _blocks_text(action.rich_message.blocks)
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_rich_draft_content_only_untagged_when_inactive(auto_home):
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_draft_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_content("Hello **world**")
        await async_wait_until(lambda: len(_rich_draft_actions(client)) == 1)

        action = _rich_draft_actions(client)[0]
        assert type(action.rich_message).__name__ == "InputRichMessageMarkdown"
    finally:
        sink.cancel()


def test_sync_draft_shape_never_tagged(auto_home):
    autonomous_state.set_active(auto_home, True)
    sink = ResponseStreamDraft(_fake_draft_adapter(_DraftClient()), "77")

    action = sink._build_action("rich", "final text", can_stop=False, include_thinking=False)

    assert type(action.rich_message).__name__ == "InputRichMessageMarkdown"


def test_text_draft_never_tagged(auto_home):
    autonomous_state.set_active(auto_home, True)
    sink = ResponseStreamDraft(_fake_draft_adapter(_DraftClient()), "77")

    action = sink._build_action("text", "Thinking")

    assert type(action).__name__ == "SendMessageTextDraftAction"


# ---------------------------------------------------------------------------
# Adapter send(): flag in metadata, first part only
# ---------------------------------------------------------------------------

class _SendClient:
    """Fake Telethon client capturing SendMessageRequests."""

    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=900)


@pytest.mark.asyncio
async def test_adapter_send_tags_first_part_only():
    adapter = TelethonAdapter(PlatformConfig())
    client = _SendClient()
    adapter._client = client
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))

    result = await adapter.send(
        "42", "x" * 33000, metadata={"telegram_autonomous_tag": True})

    assert result.success
    assert len(client.requests) == 2
    first = client.requests[0].rich_message
    assert type(first).__name__ == "InputRichMessage"
    assert _is_tag_row(first.blocks[0])
    assert type(client.requests[1].rich_message).__name__ == "InputRichMessageMarkdown"


@pytest.mark.asyncio
async def test_adapter_send_without_flag_is_untagged():
    adapter = TelethonAdapter(PlatformConfig())
    client = _SendClient()
    adapter._client = client
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(42, 0))

    result = await adapter.send("42", "Hello **world**", metadata={})

    assert result.success
    assert len(client.requests) == 1
    assert type(client.requests[0].rich_message).__name__ == "InputRichMessageMarkdown"


# ---------------------------------------------------------------------------
# Final delivery bracket
# ---------------------------------------------------------------------------

def _final_event(*, chat_type="dm", thread_id=None):
    return SimpleNamespace(
        source=_dm_source(chat_type=chat_type, thread_id=thread_id),
        text="final answer", message_id="in-1")


def _final_adapter():
    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="<redacted>", extra={}))
    runner = MagicMock()
    runner._delivery_adapter_for = MagicMock(return_value=adapter)
    adapter.gateway_runner = runner
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="900"))
    return adapter


@pytest.mark.asyncio
async def test_send_final_ledgered_tags_dm_final_when_active(auto_home):
    autonomous_state.set_active(auto_home, True)
    adapter = _final_adapter()

    result, _ = await adapter.send_final_ledgered(
        _final_event(), "k", "final answer", {}, reply_to=None)

    assert result.success
    metadata = adapter.send.await_args.kwargs.get("metadata", {})
    assert metadata.get("telegram_autonomous_tag") is True


@pytest.mark.asyncio
async def test_send_final_ledgered_leaves_group_final_untagged(auto_home):
    autonomous_state.set_active(auto_home, True)
    adapter = _final_adapter()

    result, _ = await adapter.send_final_ledgered(
        _final_event(chat_type="group"), "k", "final answer", {}, reply_to=None)

    assert result.success
    metadata = adapter.send.await_args.kwargs.get("metadata", {})
    assert "telegram_autonomous_tag" not in metadata


@pytest.mark.asyncio
async def test_send_final_ledgered_leaves_final_untagged_when_inactive(auto_home):
    adapter = _final_adapter()

    result, _ = await adapter.send_final_ledgered(
        _final_event(), "k", "final answer", {}, reply_to=None)

    assert result.success
    metadata = adapter.send.await_args.kwargs.get("metadata", {})
    assert "telegram_autonomous_tag" not in metadata
