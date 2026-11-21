"""Behaviour contracts for the streaming draft thinking window.

While a DM turn streams reasoning, the draft shows a "(Thinking Ns)"
prefix (N = whole seconds since the first delta arrived) above a
sliding window of the last 7 reasoning lines: the 8th line clears the
1st, the 9th clears the 2nd, until the agent responds with actual
content. There is no hidden-line count. The static "Thinking" prime
is unchanged, and the final content carries no prefix — the sync draft
before the final send stays the plain first chunk of the final.
"""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telethon import types

from gateway import autonomous_state
from gateway.config import PlatformConfig
from plugins.platforms.telegram.streaming_response import ResponseStreamDraft
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from tests.waiting import async_wait_until


@pytest.fixture
def auto_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


class _DraftClient:
    """Fake Telethon client capturing SetTypingRequests."""

    def __init__(self):
        self.sent = []

    async def __call__(self, request):
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


class _Clock:
    """Controllable monotonic clock for the sink's time_fn."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _thinking_texts(client):
    """Thinking-block text of every rich draft action, in send order."""
    texts = []
    for request in client.sent:
        if type(request).__name__ != "SetTypingRequest":
            continue
        action = request.action
        if type(action).__name__ != "InputSendMessageRichMessageDraftAction":
            continue
        rich = action.rich_message
        if type(rich).__name__ != "InputRichMessage":
            continue
        for block in rich.blocks or []:
            if type(block).__name__ == "PageBlockThinking":
                node = block.text
                while node is not None and not isinstance(node, str):
                    node = getattr(node, "text", None)
                texts.append(node or "")
    return texts


def _rich_actions(client):
    """Rich draft actions (InputSendMessageRichMessageDraftAction), in order."""
    actions = []
    for request in client.sent:
        if type(request).__name__ != "SetTypingRequest":
            continue
        action = request.action
        if type(action).__name__ == "InputSendMessageRichMessageDraftAction":
            actions.append(action)
    return actions


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


def _thinking_prefix(text):
    """Leading "(Thinking Ns) " prefix of a draft text, or None."""
    match = re.match(r"^\(Thinking \d+s\) ", text)
    return match.group(0) if match else None


def _make_sink(client, clock):
    loop = asyncio.get_running_loop()
    return ResponseStreamDraft(_fake_draft_adapter(client), "77", loop=loop,
                               min_interval_s=0, time_fn=clock)


@pytest.mark.asyncio
async def test_reasoning_draft_shows_last_seven_lines_with_prefix(auto_home):
    """Eight reasoning lines: the draft shows the last seven under the
    "(Thinking Ns)" prefix; the 8th line clears the 1st."""
    client = _DraftClient()
    clock = _Clock(1000.0)
    sink = _make_sink(client, clock)
    try:
        for i in range(1, 9):
            sink.on_reasoning(f"line{i}\n")
        await async_wait_until(lambda: len(_thinking_texts(client)) >= 8)
        last = _thinking_texts(client)[-1]
        assert last.startswith("(Thinking 0s) ")
        body = last[len("(Thinking 0s) "):]
        assert body == "\n".join(f"line{i}" for i in range(2, 9))
        assert "line1" not in body
        # The 9th line clears the 2nd; the window keeps sliding.
        sink.on_reasoning("line9\n")
        await async_wait_until(lambda: len(_thinking_texts(client)) >= 9)
        last = _thinking_texts(client)[-1]
        assert last.startswith("(Thinking 0s) ")
        assert last[len("(Thinking 0s) "):] == "\n".join(
            f"line{i}" for i in range(3, 10))
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_thinking_seconds_tick_as_deltas_arrive(auto_home):
    """N counts whole seconds since the first delta arrived, and updates
    as deltas arrive — it is not a standalone ticker."""
    client = _DraftClient()
    clock = _Clock(1000.0)
    sink = _make_sink(client, clock)
    try:
        sink.on_reasoning("line1\n")
        await async_wait_until(lambda: len(_thinking_texts(client)) >= 1)
        assert _thinking_texts(client)[-1].startswith("(Thinking 0s) ")
        clock.now = 1003.5
        sink.on_reasoning("line2\n")
        await async_wait_until(lambda: len(_thinking_texts(client)) >= 2)
        assert _thinking_texts(client)[-1].startswith("(Thinking 3s) ")
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_prefix_persists_until_content_arrives(auto_home):
    """The prefix stays on every reasoning draft; once content flows the
    draft shows the windowed reasoning above the content with no prefix."""
    client = _DraftClient()
    clock = _Clock(1000.0)
    sink = _make_sink(client, clock)
    try:
        for i in range(1, 9):
            sink.on_reasoning(f"line{i}\n")
        await async_wait_until(lambda: len(_thinking_texts(client)) >= 8)
        assert all(t.startswith("(Thinking 0s) ") for t in _thinking_texts(client))
        sink.on_content("Hello there")
        await async_wait_until(lambda: len(_rich_actions(client)) >= 9)
        action = _rich_actions(client)[-1]
        thinking = [b for b in action.rich_message.blocks
                    if type(b).__name__ == "PageBlockThinking"]
        assert len(thinking) == 1
        node = thinking[0].text
        while node is not None and not isinstance(node, str):
            node = getattr(node, "text", None)
        assert node == "\n".join(f"line{i}" for i in range(2, 9))
        assert _thinking_prefix(node or "") is None
        assert "Hello there" in _blocks_text(action.rich_message.blocks)
        assert not any(_thinking_prefix(t) for t in _thinking_texts(client)[-1:])
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_sync_draft_is_plain_final_without_prefix(auto_home):
    """The last draft carries the thinking prefix, but the sync draft
    before the final send is the plain first chunk of the final — the
    prefix never leaks into finalisation."""
    client = _DraftClient()
    clock = _Clock(1000.0)
    sink = _make_sink(client, clock)
    try:
        sink.on_reasoning("line1\nline2\n")
        await async_wait_until(lambda: len(_thinking_texts(client)) >= 1)
        assert _thinking_texts(client)[-1].startswith("(Thinking 0s) ")
        # finish_sync_draft blocks on the loop from the worker thread in
        # production; run it off-loop here the same way.
        await asyncio.to_thread(sink.finish_sync_draft, "Actual final answer")
        syncs = [a for a in _rich_actions(client) if a.can_stop is False]
        assert len(syncs) == 1
        assert syncs[0].rich_message.markdown == "Actual final answer"
        assert _thinking_prefix(syncs[0].rich_message.markdown or "") is None
    finally:
        sink.cancel()
