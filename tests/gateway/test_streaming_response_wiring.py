"""Streaming response wiring: the turn attaches the adapter's rich draft sink.

The rich draft sink rides the agent's delta callbacks for a non-guest DM
turn with the Beta setting on; no GatewayStreamConsumer is created for it
(display streaming is off by default), and the final still goes out
through the normal delivery path. Groups and guest threads never get it.
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig, StreamingConfig
from gateway.run_turn_runner import TurnRunner
from gateway.stream_consumer import streaming_response_sink_for_turn
from gateway.turn_context import TurnContext
from plugins.platforms.telegram.streaming_response import ResponseStreamDraft
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from tests.waiting import async_wait_until


class _DraftClient:
    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace()


def _draft_frames(client):
    frames = []
    for request in client.requests:
        if type(request).__name__ != "SetTypingRequest":
            continue
        action = request.action
        if type(action).__name__ == "InputSendMessageRichMessageDraftAction":
            rich = action.rich_message
            if type(rich).__name__ == "InputRichMessage":
                frames.append(("thinking", rich.blocks[0].text.text))
            else:
                frames.append(("rich", rich.markdown))
        else:
            frames.append(("text", action.text.text))
    return frames


def _dm_turn(*, streaming, chat_type="dm", thread_id=None):
    loop = asyncio.get_running_loop()
    client = _DraftClient()
    adapter = TelethonAdapter(PlatformConfig(
        extra={"streaming_response": streaming} if streaming else {}))
    adapter._client = client
    # The sink only sends with a validated InputPeer; the fake client cannot
    # resolve one, so seed the adapter's peer cache as an inbound DM would.
    from telethon import types as _tl_types
    adapter._peer_cache = {"77": _tl_types.InputPeerUser(77, 123)}
    ctx = TurnContext(
        streaming_tts_consumer_holder=[None], user_config={},
        resolve_display_setting=lambda *args: None,
        interim_assistant_messages_enabled=False,
        source=SimpleNamespace(platform=Platform.TELEGRAM, chat_id="77",
                               thread_id=thread_id, chat_type=chat_type),
        _run_still_current=lambda: True,
    )
    ctx._loop_for_step = loop
    runner = SimpleNamespace(
        config=SimpleNamespace(streaming=StreamingConfig()),
        _delivery_adapter_for=lambda source: adapter,
        _build_stream_consumer_config=lambda *args, **kwargs: (None, None),
    )
    turn = TurnRunner(runner, ctx)
    return turn, client


@pytest.mark.asyncio
async def test_setup_stream_consumer_wires_streaming_response_sink():
    turn, client = _dm_turn(streaming=True)

    consumer, delta_cb, _interim, want_interim, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert consumer is None
    assert want_interim is False
    assert delta_cb is not None
    assert reasoning_cb is not None
    reasoning_cb("Thinking through it")
    delta_cb("Hello DM!")
    # The content frame is trailing-edge held behind the 1 s gate; the pump
    # flushes it once the gate opens.
    await async_wait_until(lambda: len(_draft_frames(client)) == 2)
    assert _draft_frames(client) == [("thinking", "Thinking through it"), ("rich", "Hello DM!")]


@pytest.mark.asyncio
async def test_setup_stream_consumer_skips_response_sink_for_group():
    turn, _client = _dm_turn(streaming=True, chat_type="group")

    consumer, delta_cb, _interim, want_interim, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert consumer is None
    assert delta_cb is None
    assert reasoning_cb is None
    assert want_interim is False


@pytest.mark.asyncio
async def test_setup_stream_consumer_skips_response_sink_for_guest_thread():
    turn, _client = _dm_turn(streaming=True, thread_id="guest-77")

    consumer, delta_cb, _interim, want_interim, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert consumer is None
    assert delta_cb is None
    assert reasoning_cb is None
    assert want_interim is False


@pytest.mark.asyncio
async def test_setup_stream_consumer_skips_response_sink_when_streaming_off():
    turn, _client = _dm_turn(streaming=False)

    consumer, delta_cb, _interim, want_interim, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert consumer is None
    assert delta_cb is None
    assert reasoning_cb is None
    assert want_interim is False


def test_streaming_response_sink_for_turn_returns_none_without_adapter():
    ctx = SimpleNamespace(source=SimpleNamespace(thread_id=None, chat_type="dm"))
    runner = SimpleNamespace(_delivery_adapter_for=lambda source: None)
    assert streaming_response_sink_for_turn(runner, ctx) is None

    runner = SimpleNamespace(_delivery_adapter_for=lambda source: SimpleNamespace())
    assert streaming_response_sink_for_turn(runner, ctx) is None

    def _raising(_source, _loop):
        raise RuntimeError("boom")

    runner = SimpleNamespace(
        _delivery_adapter_for=lambda source: SimpleNamespace(streaming_response_sink=_raising))
    assert streaming_response_sink_for_turn(runner, ctx) is None


def test_streaming_response_sink_for_turn_returns_sink_for_dm():
    adapter = TelethonAdapter(PlatformConfig(extra={"streaming_response": True}))
    ctx = SimpleNamespace(source=SimpleNamespace(chat_id="77", thread_id=None, chat_type="dm"))
    runner = SimpleNamespace(_delivery_adapter_for=lambda source: adapter)
    sink = streaming_response_sink_for_turn(runner, ctx)
    assert isinstance(sink, ResponseStreamDraft)


@pytest.mark.asyncio
async def test_streaming_response_sink_attach_logs_at_info(caplog):
    """The next live test must show from logs alone whether a DM turn got a
    draft sink — so the attach is INFO, not debug."""
    turn, _client = _dm_turn(streaming=True)
    with caplog.at_level(logging.INFO, logger="gateway.stream_consumer"):
        turn._setup_stream_consumer("telegram")
    assert "Streaming response draft sink attached" in caplog.text
    assert "thread=None" in caplog.text
