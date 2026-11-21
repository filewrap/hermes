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


def _blocks_text(blocks):
    """Plain text of rich blocks, skipping any Thinking block."""
    out = []

    def _walk(node):
        if node is None:
            return
        if type(node).__name__ == "PageBlockThinking":
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


def _rich_text(node):
    """Innermost plain string of a RichText node: a bare ``TextPlain`` or a
    styled wrapper such as ``TextItalic(TextPlain(...))``."""
    text = getattr(node, "text", None)
    while text is not None and not isinstance(text, str):
        text = getattr(text, "text", None)
    return text


def _draft_frames(client):
    frames = []
    for request in client.requests:
        if type(request).__name__ != "SetTypingRequest":
            continue
        action = request.action
        if type(action).__name__ == "InputSendMessageRichMessageDraftAction":
            rich = action.rich_message
            if type(rich).__name__ == "InputRichMessage":
                blocks = list(rich.blocks)
                if len(blocks) == 1 and type(blocks[0]).__name__ == "PageBlockThinking":
                    frames.append(("thinking", _rich_text(blocks[0].text)))
                else:
                    frames.append(("rich", _blocks_text(blocks)))
            else:
                frames.append(("rich", rich.markdown))
        else:
            frames.append(("text", action.text.text))
    return frames


def _thinking_blocks(client):
    """Every Thinking block sent as a draft, in order."""
    blocks = []
    for request in client.requests:
        if type(request).__name__ != "SetTypingRequest":
            continue
        action = request.action
        if type(action).__name__ != "InputSendMessageRichMessageDraftAction":
            continue
        rich = action.rich_message
        if type(rich).__name__ != "InputRichMessage":
            continue
        blocks.extend(b for b in rich.blocks if type(b).__name__ == "PageBlockThinking")
    return blocks


def _dm_turn(*, streaming, chat_type="dm", thread_id=None, show_reasoning=False,
             session_key=None):
    loop = asyncio.get_running_loop()
    client = _DraftClient()
    adapter = TelethonAdapter(PlatformConfig(
        extra={"streaming_response": streaming} if streaming else {}))
    adapter._client = client
    # The sink only sends with a validated InputPeer; the fake client cannot
    # resolve one, so seed the adapter's peer cache as an inbound DM would.
    from telethon import types as _tl_types
    adapter._peer_cache = {"77": _tl_types.InputPeerUser(77, 123)}
    user_config = (
        {"display": {"platforms": {"telegram": {"show_reasoning": True}}}}
        if show_reasoning else {})
    ctx = TurnContext(
        streaming_tts_consumer_holder=[None], user_config=user_config,
        resolve_display_setting=lambda *args: None,
        interim_assistant_messages_enabled=False, session_key=session_key,
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
    turn, client = _dm_turn(streaming=True, show_reasoning=True)

    consumer, delta_cb, _interim, want_interim, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert consumer is None
    assert want_interim is False
    assert delta_cb is not None
    assert reasoning_cb is not None
    # The primed placeholder lands first; the first reasoning delta
    # leaves the LOAD lane immediately, then the first content frame.
    await async_wait_until(lambda: len(_draft_frames(client)) == 1)
    reasoning_cb("Thinking through it")
    await async_wait_until(lambda: len(_draft_frames(client)) == 2)
    delta_cb("Hello DM!")
    # The first content frame leaves the reasoning lane immediately (lane
    # shift), so all three frames land without waiting on the 1 s gate.
    await async_wait_until(lambda: len(_draft_frames(client)) == 3)
    assert _draft_frames(client) == [("text", "Thinking"),
                                     ("thinking", "(Thinking 0s) Thinking through it"),
                                     ("rich", "Hello DM!")]


@pytest.mark.asyncio
async def test_setup_stream_consumer_primes_thinking_placeholder():
    """Turn setup primes the plain "Thinking" draft before any
    reasoning arrives — a draft action only, never a sent message."""
    turn, client = _dm_turn(streaming=True, show_reasoning=False)

    _consumer, _delta_cb, _interim, _want, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert reasoning_cb is None  # Display off: no reasoning reaches the draft
    await async_wait_until(lambda: len(_draft_frames(client)) == 1)
    assert _draft_frames(client) == [("text", "Thinking")]
    assert {type(r).__name__ for r in client.requests} == {"SetTypingRequest"}
    action = next(r.action for r in client.requests)
    assert type(action).__name__ == "SendMessageTextDraftAction"
    assert action.text.text == "Thinking"


@pytest.mark.asyncio
async def test_setup_stream_consumer_pauses_typing_refresh_for_streaming_turn():
    """A streaming DM turn pauses the 2 s typing refresh: its plain typing
    action would overwrite the animated draft action every tick. A
    non-streaming turn pauses nothing."""
    turn, _client = _dm_turn(streaming=True)
    turn._setup_stream_consumer("telegram")
    adapter = turn._runner._delivery_adapter_for(turn._ctx.source)
    assert "77" in adapter._typing_paused

    plain_turn, _plain_client = _dm_turn(streaming=False)
    plain_turn._setup_stream_consumer("telegram")
    plain_adapter = plain_turn._runner._delivery_adapter_for(plain_turn._ctx.source)
    assert "77" not in plain_adapter._typing_paused


@pytest.mark.asyncio
async def test_setup_stream_consumer_placeholder_replaced_by_first_reasoning():
    """The first reasoning delta replaces the placeholder draft with the
    live reasoning draft (same sink, same random_id)."""
    turn, client = _dm_turn(streaming=True, show_reasoning=True)

    _consumer, delta_cb, _interim, _want, reasoning_cb = turn._setup_stream_consumer("telegram")

    await async_wait_until(lambda: len(_draft_frames(client)) == 1)
    assert _draft_frames(client) == [("text", "Thinking")]
    reasoning_cb("Thinking through it")
    await async_wait_until(lambda: len(_draft_frames(client)) == 2)
    assert _draft_frames(client)[1] == ("thinking", "(Thinking 0s) Thinking through it")
    delta_cb("Hello DM!")
    await async_wait_until(lambda: len(_draft_frames(client)) == 3)
    assert _draft_frames(client) == [("text", "Thinking"),
                                     ("thinking", "(Thinking 0s) Thinking through it"),
                                     ("rich", "Hello DM!")]


@pytest.mark.asyncio
async def test_setup_stream_consumer_no_placeholder_when_streaming_off():
    """A non-streaming turn sends no placeholder draft at all — no sink,
    no draft of any kind."""
    turn, client = _dm_turn(streaming=False)

    _consumer, _delta_cb, _interim, _want, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert reasoning_cb is None
    assert client.requests == []


@pytest.mark.asyncio
async def test_setup_stream_consumer_hides_draft_reasoning_when_display_off():
    """Display off + streaming on: the draft carries content only — no
    Thinking block, no reasoning anywhere in the draft. The final message
    carries no Reasoning section either (see the matrix test): with
    Display off the reasoning is shown nowhere."""
    turn, client = _dm_turn(streaming=True, show_reasoning=False)

    _consumer, delta_cb, _interim, _want, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert reasoning_cb is None
    delta_cb("Hello DM!")
    # The primed placeholder goes first; the first content draft leaves
    # the LOAD lane immediately. No Thinking block anywhere: Display is off.
    await async_wait_until(lambda: len(_draft_frames(client)) == 2)
    assert _draft_frames(client) == [("text", "Thinking"), ("rich", "Hello DM!")]
    assert _thinking_blocks(client) == []


@pytest.mark.asyncio
async def test_setup_stream_consumer_shows_italic_draft_reasoning_when_display_on():
    """Display on + streaming on: the reasoning reaches the draft, in an
    italic Thinking block — the draft-only formation."""
    turn, client = _dm_turn(streaming=True, show_reasoning=True)

    _consumer, delta_cb, _interim, _want, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert reasoning_cb is not None
    await async_wait_until(lambda: len(_draft_frames(client)) == 1)
    reasoning_cb("Thinking through it")
    await async_wait_until(lambda: len(_draft_frames(client)) == 2)
    delta_cb("Hello DM!")
    await async_wait_until(lambda: len(_draft_frames(client)) == 3)
    assert _draft_frames(client) == [("text", "Thinking"),
                                     ("thinking", "(Thinking 0s) Thinking through it"),
                                     ("rich", "Hello DM!")]
    blocks = _thinking_blocks(client)
    assert len(blocks) == 2
    for block in blocks:
        assert type(block.text).__name__ == "TextItalic"
    assert _rich_text(blocks[0].text) == "(Thinking 0s) Thinking through it"
    assert _rich_text(blocks[1].text) == "Thinking through it"


@pytest.mark.asyncio
async def test_reasoning_shown_once_draft_has_it_final_does_not(monkeypatch):
    """Shown once: Display on + streaming on puts the reasoning in the
    draft (italic Thinking block) and the final message carries no
    Reasoning section repeating it."""
    import gateway.run as gateway_run
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    turn, client = _dm_turn(streaming=True, show_reasoning=True)
    _consumer, _delta_cb, _interim, _want, reasoning_cb = turn._setup_stream_consumer("telegram")
    await async_wait_until(lambda: len(_draft_frames(client)) == 1)
    reasoning_cb("Thinking through it")
    await async_wait_until(lambda: len(_draft_frames(client)) == 2)
    assert _draft_frames(client)[1] == ("thinking", "(Thinking 0s) Thinking through it")

    monkeypatch.setattr(
        gateway_run, "_load_gateway_config",
        lambda: {"display": {"show_reasoning": True}})
    adapter = turn._runner._delivery_adapter_for(turn._ctx.source)
    mixin = GatewayTurnPersistenceMixin()
    mixin._delivery_adapter_for = lambda source: adapter
    reply = mixin._hmwa_prepend_reasoning(
        {"last_reasoning": "Thinking through it"}, "Hello DM!", turn._ctx.source, False)
    assert reply == "Hello DM!"


@pytest.mark.asyncio
async def test_setup_stream_consumer_wires_stream_control_hooks(caplog):
    """_setup_stream_consumer assigns the control-framework hooks
    alongside on_stop: lane shift, stall wait, and end. The wait reaches
    the agent's diagnostic-wait path; the shift and the end are INFO so
    the live sequence is visible in host logs."""
    turn, _client = _dm_turn(streaming=True, show_reasoning=True)

    class _FakeAgent:
        def __init__(self):
            self.wait_notices = []

        def _emit_diagnostic_wait(self, text):
            self.wait_notices.append(text)

    agent = _FakeAgent()
    turn._ctx.agent_holder[0] = agent
    turn._setup_stream_consumer("telegram")
    sink = turn._response_stream_sink
    assert sink is not None
    assert sink.on_lane_shift is not None
    assert sink.on_wait is not None
    assert sink.on_end is not None
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        sink.on_lane_shift("reasoning", "content")
        sink.on_end("Hello DM!", False)
    assert "lane shift" in caplog.text
    assert "reasoning" in caplog.text and "content" in caplog.text
    assert "Streaming response ended" in caplog.text
    sink.on_wait(5.0)
    assert len(agent.wait_notices) == 1
    assert "5s" in agent.wait_notices[0]


@pytest.mark.asyncio
async def test_setup_stream_consumer_registers_post_delivery_draft_clear():
    """Turn setup registers the post-final draft clear on the adapter:
    after the final send confirms, one CancelAction ends the live draft
    at once instead of lingering to the TTL."""
    turn, client = _dm_turn(streaming=True, session_key="test-session")
    turn._setup_stream_consumer("telegram")
    adapter = turn._runner._delivery_adapter_for(turn._ctx.source)
    assert "test-session" in adapter._post_delivery_callbacks
    # Firing the registered callback clears the draft that was shown.
    await async_wait_until(lambda: len(_draft_frames(client)) == 1)
    callback = adapter.pop_post_delivery_callback("test-session")
    assert callback is not None
    from gateway.platforms.base_post_delivery import run_post_delivery_callback
    await run_post_delivery_callback(callback, 30.0)
    cancels = [r for r in client.requests
               if type(r).__name__ == "SetTypingRequest"
               and type(r.action).__name__ == "SendMessageCancelAction"]
    assert len(cancels) == 1


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


@pytest.mark.asyncio
async def test_streaming_stop_interrupt_calls_agent_interrupt():
    """The sink's on_stop (wired by _setup_stream_consumer) halts the
    generation through the agent's thread-safe interrupt."""
    turn, _client = _dm_turn(streaming=True)

    class _FakeAgent:
        def __init__(self):
            self.interrupts = []

        def interrupt(self, message=None, **kwargs):
            self.interrupts.append((message, kwargs))

    agent = _FakeAgent()
    turn._ctx.agent_holder[0] = agent
    consumer, _delta_cb, _interim, _want, _reasoning_cb = turn._setup_stream_consumer("telegram")
    assert consumer is None
    sink = turn._response_stream_sink
    assert sink is not None
    assert sink.on_stop is not None

    sink.on_stop()
    assert agent.interrupts == [("Stopped from Telegram", {"hard_cancel": True})]


@pytest.mark.asyncio
async def test_finish_stream_consumer_delivers_stopped_partial():
    """A Stop press interrupts the turn, so the result carries the
    diagnostic final — the streamed partial is delivered as the final
    instead, and the sync draft adopts it. DM turns have no
    GatewayStreamConsumer (stream_consumer=None): the sink path must
    still run."""
    turn, client = _dm_turn(streaming=True, show_reasoning=True)
    consumer, delta_cb, _interim, _want, reasoning_cb = turn._setup_stream_consumer("telegram")
    assert consumer is None
    sink = turn._response_stream_sink
    assert sink is not None
    reasoning_cb("Thinking through it")
    await async_wait_until(lambda: len(_draft_frames(client)) == 2)
    delta_cb("Hello DM!")
    assert sink.partial_text == "Hello DM!"
    # Let the placeholder + reasoning + content frames actually land before
    # the Stop, as in a real stream: the last draft then leads with the
    # Thinking text, so a plain sync draft is needed for the client to adopt
    # the final over it. Each frame leaves its lane immediately, so each
    # lands as its own frame.
    await async_wait_until(lambda: len(_draft_frames(client)) == 3)

    sink.request_stop()
    assert sink.stop_requested

    result = {"interrupted": True, "completed": False,
              "final_response": "turn interrupted by user"}
    # _finish_stream_consumer blocks on the sync draft's bounded wait, so
    # it runs off the event loop — as in production, where the turn thread
    # and the adapter loop are different threads.
    await asyncio.to_thread(turn._finish_stream_consumer, result, [], None)
    assert result["final_response"] == "Hello DM!"
    # The sync draft is the plain final chunk (no Thinking block, no
    # Stop button) so the client adopts the final over the draft.
    frames = _draft_frames(client)
    assert frames and frames[-1] == ("rich", "Hello DM!")
