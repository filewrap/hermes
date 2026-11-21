"""Live draft demos for /draft (topics 2 and 6): behaviour contracts.

The demos drive the REAL ``ResponseStreamDraft`` sink against a fake
MTProto client that records every request, so the frame sequence is
observed on the wire shape — placeholder text draft, italic Thinking
reasoning draft, rich content draft, sync draft — and the final message
goes out as a real ``SendMessageRequest``. No demo frame is ever a sent
message.

The injected ``sleep`` stands in for the production frame delay: it
records the delay the demo asked for and waits for the frame that
preceded it to land (event-based, via ``async_wait_until``), never for
the clock — the sink's own 1 s trailing-edge throttle still paces the
frames, as in production.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telethon import types as _tl_types

from gateway.config import PlatformConfig
from gateway.draft_demo import (
    DEMO_CONTENT_TOPIC2,
    DEMO_FINAL_TOPIC2,
    DEMO_REASONING_TOPIC2,
    DEMO_REASONING_TOPIC6,
    DRAFT_ACTION_NAMES,
    NON_DRAFT_ACTIONS,
    build_action,
    preview_next,
    run_demo_topic2,
    run_demo_topic6,
    send_animation_message,
    show_action,
    show_content,
    show_placeholder,
    show_reasoning,
    show_stop_draft,
    show_sync_and_final,
)
from plugins.platforms.telegram.streaming_response import ResponseStreamDraft
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from tests.waiting import async_wait_until


class _DraftClient:
    """Fake MTProto client: records every request, answers with an empty receipt."""

    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace()


def _make_adapter(*, streaming=False, peer_cached=True):
    adapter = TelethonAdapter(PlatformConfig(
        extra={"streaming_response": streaming} if streaming else {}))
    adapter._client = _DraftClient()
    # The sink only sends with a validated InputPeer; the fake client
    # cannot resolve one, so seed the adapter's peer cache as an inbound
    # DM would — unless the test wants resolution to fail.
    adapter._peer_cache = {"77": _tl_types.InputPeerUser(77, 123)} if peer_cached else {}
    return adapter


def _dm_source(chat_type="dm"):
    return SimpleNamespace(chat_id="77", chat_type=chat_type)


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
    """Draft frames in send order: (kind, text) per SetTypingRequest."""
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
            # A raw typing action (typing, upload, record, ...) is not
            # a draft frame: it carries no text, so skip it rather than
            # crashing the frame parse.
            text = getattr(action, "text", None)
            if text is not None:
                frames.append(("text", text.text))
    return frames


def _draft_stop_flags(client):
    """can_stop per draft frame, in send order — the sync draft (turn
    over) carries no Stop button, every live frame does."""
    return [request.action.can_stop for request in client.requests
            if type(request).__name__ == "SetTypingRequest"]


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


def _sent_messages(client):
    """Plain sent messages (SendMessageRequest), in order — never a draft."""
    return [r for r in client.requests if type(r).__name__ == "SendMessageRequest"]


def _wait_frames_sleep(client, calls, target):
    """Injected sleep for one step: record the delay, then wait until
    ``target`` draft frames have landed (the sink's 1 s throttle paces
    the flush, as in production)."""
    async def _sleep(delay):
        calls.append(delay)
        await async_wait_until(
            lambda: len(_draft_frames(client)) >= target,
            what=f"{target} draft frame(s) shown")
    return _sleep


def _show_sleep(client, calls):
    """Injected sleep for topic 6: record the delay, then wait until the
    reasoning draft is visible."""
    async def _sleep(delay):
        calls.append(delay)
        await async_wait_until(
            lambda: len(_draft_frames(client)) == 1,
            what="reasoning draft shown")
    return _sleep


@pytest.mark.asyncio
async def test_topic2_live_sequence_with_streaming_off():
    """Topic 2 works with the streaming setting OFF (each step builds its
    own sink): placeholder -> reasoning -> content -> sync + final
    message. No frame is a sent message.

    One fresh sink per step, so the wire sequence differs from the old
    single-sink demo: the reasoning frame shows three times (once in the
    reasoning step, once as the first frame of the content step, once as
    the sync step's prime), there are four sink random_ids, and the sync
    step's prime lets its sync draft fire — a plain rich draft of the
    final, no Stop button — before the final message. The final message
    is still exactly one plain send, after all draft frames."""
    adapter = _make_adapter(streaming=False)
    client = adapter._client
    calls = []

    def _topic2_sleep(client, calls):
        # Hold 1 -> placeholder frame; hold 2 -> reasoning frame;
        # hold 3 -> both content-step frames (thinking, then combined);
        # hold 4 -> the sync step's reasoning prime.
        targets = [1, 2, 4, 5]

        async def _sleep(delay):
            calls.append(delay)
            target = targets[len(calls) - 1]
            await async_wait_until(
                lambda: len(_draft_frames(client)) >= target,
                what=f"demo frame {target} landed")
        return _sleep

    result = await run_demo_topic2(
        adapter, _dm_source(), sleep=_topic2_sleep(client, calls))

    assert result is True
    assert calls == [10.0, 10.0, 10.0, 10.0]
    assert _draft_frames(client) == [
        ("text", "Thinking"),
        ("thinking", "(Thinking 0s) " + DEMO_REASONING_TOPIC2),
        ("thinking", "(Thinking 0s) " + DEMO_REASONING_TOPIC2),
        ("rich", DEMO_CONTENT_TOPIC2),
        ("thinking", "(Thinking 0s) " + DEMO_REASONING_TOPIC2),
        ("rich", DEMO_FINAL_TOPIC2),
    ]
    # The prime and every live frame carry the Stop button; the sync
    # draft (turn over) does not.
    assert _draft_stop_flags(client) == [True, True, True, True, True, False]
    # The reasoning shows italic, in every thinking frame and frozen
    # above the content frame — draft-only formation, never in the final.
    # Thinking frames lead with the "(Thinking Ns)" prefix; the frozen
    # reasoning above the content frame does not (content has arrived).
    blocks = _thinking_blocks(client)
    assert len(blocks) == 4
    for block in blocks:
        assert type(block.text).__name__ == "TextItalic"
    assert _rich_text(blocks[0].text) == "(Thinking 0s) " + DEMO_REASONING_TOPIC2
    assert _rich_text(blocks[1].text) == "(Thinking 0s) " + DEMO_REASONING_TOPIC2
    assert _rich_text(blocks[2].text) == DEMO_REASONING_TOPIC2
    assert _rich_text(blocks[3].text) == "(Thinking 0s) " + DEMO_REASONING_TOPIC2
    # Exactly one sent message, the final, after all six draft frames.
    sent = _sent_messages(client)
    assert len(sent) == 1
    assert sent[0].message == DEMO_FINAL_TOPIC2
    assert client.requests[-1] is sent[0]
    # One sink per step: four draft sinks, one random_id each; the
    # final send is a different request entirely. Each step prunes the
    # previous step's finished sink, so one finished sink remains.
    typing = [r for r in client.requests if type(r).__name__ == "SetTypingRequest"]
    assert len(typing) == 6
    assert len({r.action.random_id for r in typing}) == 4
    assert len(adapter._streaming_sinks) == 1
    assert adapter._streaming_sinks  # registered for Stop routing
    sink = next(iter(adapter._streaming_sinks.values()))
    assert sink._finished  # pump stopped at the end of the demo


@pytest.mark.asyncio
async def test_topic6_shows_reasoning_then_cancels_with_no_message():
    """Topic 6: the italic reasoning draft shows, the sink is cancelled,
    and NO message is sent — the reasoning never reaches a final."""
    adapter = _make_adapter(streaming=True)
    client = adapter._client
    calls = []

    result = await run_demo_topic6(
        adapter, _dm_source(), sleep=_show_sleep(client, calls))

    assert result is True
    assert calls == [10.0]
    assert _draft_frames(client) == [("thinking", "(Thinking 0s) " + DEMO_REASONING_TOPIC6)]
    blocks = _thinking_blocks(client)
    assert len(blocks) == 1
    assert type(blocks[0].text).__name__ == "TextItalic"
    assert _rich_text(blocks[0].text) == "(Thinking 0s) " + DEMO_REASONING_TOPIC6
    assert _sent_messages(client) == []
    assert {type(r).__name__ for r in client.requests} == {"SetTypingRequest"}
    sink = next(iter(adapter._streaming_sinks.values()))
    assert sink._finished  # cancelled: the draft expires on its own


@pytest.mark.asyncio
async def test_topic2_stop_halts_demo_without_final():
    """A Stop press during topic 2 halts the demo: no final message, False."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    async def _stopping_sleep(delay):
        calls.append(delay)
        await async_wait_until(
            lambda: len(_draft_frames(client)) >= 1,
            what="placeholder draft shown")
        for sink in list(adapter._streaming_sinks.values()):
            sink.request_stop()

    result = await run_demo_topic2(
        adapter, _dm_source(), sleep=_stopping_sleep)

    assert result is False
    assert calls == [10.0]
    assert _sent_messages(client) == []
    assert {type(r).__name__ for r in client.requests} == {"SetTypingRequest"}


@pytest.mark.asyncio
async def test_topic6_stop_halts_demo():
    """A Stop press during topic 6 halts the demo: False, no message."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    async def _stopping_sleep(delay):
        calls.append(delay)
        await async_wait_until(
            lambda: len(_draft_frames(client)) == 1,
            what="reasoning draft shown")
        for sink in list(adapter._streaming_sinks.values()):
            sink.request_stop()

    result = await run_demo_topic6(
        adapter, _dm_source(), sleep=_stopping_sleep)

    assert result is False
    assert calls == [10.0]
    assert _sent_messages(client) == []


@pytest.mark.asyncio
async def test_demo_returns_false_for_non_dm_without_sink():
    """Non-DM source: False immediately, no sink created, nothing sent,
    the injected sleep never called."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    async def _never_sleep(delay):
        calls.append(delay)

    assert await run_demo_topic2(
        adapter, _dm_source(chat_type="group"), sleep=_never_sleep) is False
    assert await run_demo_topic6(
        adapter, _dm_source(chat_type="group"), sleep=_never_sleep) is False
    assert calls == []
    assert client.requests == []
    assert adapter._streaming_sinks == {}


@pytest.mark.asyncio
async def test_demo_returns_false_without_live_client():
    """No live MTProto client: the sink could never send, so False with
    no sink and no requests."""
    adapter = _make_adapter()
    adapter._client = None
    calls = []

    async def _never_sleep(delay):
        calls.append(delay)

    assert await run_demo_topic2(
        adapter, _dm_source(), sleep=_never_sleep) is False
    assert await run_demo_topic6(
        adapter, _dm_source(), sleep=_never_sleep) is False
    assert calls == []
    assert adapter._streaming_sinks == {}


@pytest.mark.asyncio
async def test_topic2_unresolvable_peer_returns_false():
    """Sink failure (peer unresolvable, drafts disabled): the demo stops
    after the first delay — no frames, no final message, False."""
    adapter = _make_adapter(peer_cached=False)
    client = adapter._client
    calls = []

    async def _failing_sleep(delay):
        calls.append(delay)
        await async_wait_until(
            lambda: any(getattr(s, "_disabled", False)
                        for s in adapter._streaming_sinks.values()),
            what="demo sink disabled after peer failure")

    result = await run_demo_topic2(
        adapter, _dm_source(), sleep=_failing_sleep)

    assert result is False
    assert calls == [10.0]
    assert _draft_frames(client) == []
    assert _sent_messages(client) == []


# --- Step API -----------------------------------------------------------
# Each step is one message's worth of animation on its own fresh sink:
# preview the next message, placeholder, reasoning, content with frozen
# reasoning, then sync + final. Behaviour contracts per step.

_PREVIEW_TEXT = "Next: draft actions…"
_REASONING_TEXT = "Preview reasoning: what the draft will show next."
_CONTENT_TEXT = "Preview content: the streamed answer, shown live."
_FINAL_TEXT = "Preview complete — this is the final message."


async def _call_preview(adapter, source, sleep):
    return await preview_next(adapter, source, _PREVIEW_TEXT, sleep=sleep)


async def _call_placeholder(adapter, source, sleep):
    return await show_placeholder(adapter, source, sleep=sleep)


async def _call_reasoning(adapter, source, sleep):
    return await show_reasoning(adapter, source, _REASONING_TEXT, sleep=sleep)


async def _call_content(adapter, source, sleep):
    return await show_content(adapter, source, _CONTENT_TEXT, sleep=sleep)


async def _call_sync_and_final(adapter, source, sleep):
    return await show_sync_and_final(adapter, source, _FINAL_TEXT, sleep=sleep)


@pytest.mark.asyncio
async def test_preview_next_shows_exact_text_as_rich_draft_and_sends_nothing():
    """preview_next: a live rich draft reading exactly the preview text,
    held for the default 1.5 s, then cancelled. No message is sent."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    result = await preview_next(
        adapter, _dm_source(), _PREVIEW_TEXT,
        sleep=_wait_frames_sleep(client, calls, 1))

    assert result is True
    assert calls == [1.5]
    assert _draft_frames(client) == [("rich", _PREVIEW_TEXT)]
    assert _sent_messages(client) == []
    assert {type(r).__name__ for r in client.requests} == {"SetTypingRequest"}
    sink = next(iter(adapter._streaming_sinks.values()))
    assert sink._finished  # cancelled: the draft expires on its own


@pytest.mark.asyncio
async def test_show_placeholder_shows_thinking_text_draft():
    """show_placeholder: the plain "Thinking" text draft, held for
    the default 10 s. No message is sent."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    result = await show_placeholder(
        adapter, _dm_source(), sleep=_wait_frames_sleep(client, calls, 1))

    assert result is True
    assert calls == [10.0]
    assert _draft_frames(client) == [("text", "Thinking")]
    assert _sent_messages(client) == []
    assert {type(r).__name__ for r in client.requests} == {"SetTypingRequest"}
    sink = next(iter(adapter._streaming_sinks.values()))
    assert sink._finished


@pytest.mark.asyncio
async def test_show_reasoning_shows_thinking_frame():
    """show_reasoning: the italic Thinking draft carrying exactly the
    given text, held for the default 10 s. No message is sent."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    result = await show_reasoning(
        adapter, _dm_source(), _REASONING_TEXT,
        sleep=_wait_frames_sleep(client, calls, 1))

    assert result is True
    assert calls == [10.0]
    assert _draft_frames(client) == [("thinking", "(Thinking 0s) " + _REASONING_TEXT)]
    blocks = _thinking_blocks(client)
    assert len(blocks) == 1
    assert type(blocks[0].text).__name__ == "TextItalic"
    assert _rich_text(blocks[0].text) == "(Thinking 0s) " + _REASONING_TEXT
    assert _sent_messages(client) == []
    sink = next(iter(adapter._streaming_sinks.values()))
    assert sink._finished


@pytest.mark.asyncio
async def test_show_content_shows_reasoning_above_content():
    """show_content: the reasoning frame first, then the combined frame
    with the frozen reasoning above the content — the real stream's
    shape, held for the default 10 s. No message is sent."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    result = await show_content(
        adapter, _dm_source(), _CONTENT_TEXT,
        sleep=_wait_frames_sleep(client, calls, 2))

    assert result is True
    assert calls == [10.0]
    assert _draft_frames(client) == [
        ("thinking", "(Thinking 0s) " + DEMO_REASONING_TOPIC2),
        ("rich", _CONTENT_TEXT),
    ]
    blocks = _thinking_blocks(client)
    assert len(blocks) == 2
    for block in blocks:
        assert type(block.text).__name__ == "TextItalic"
    assert _rich_text(blocks[0].text) == "(Thinking 0s) " + DEMO_REASONING_TOPIC2
    assert _rich_text(blocks[1].text) == DEMO_REASONING_TOPIC2
    assert _sent_messages(client) == []
    sink = next(iter(adapter._streaming_sinks.values()))
    assert sink._finished


@pytest.mark.asyncio
async def test_show_sync_and_final_shows_reasoning_prime_then_sync_draft_then_final():
    """show_sync_and_final: the fresh sink first shows the reasoning prime
    (italic Thinking draft), held for the default 10 s prime delay so
    the frame lands; only then does the sync draft fire — a plain rich
    draft equal to the first chunk of the final, with no Stop button —
    and then exactly one final message goes out. The sync frame is on
    the wire before the final send, and the prime precedes it: without
    the prime the sink never showed a draft and the sync is a no-op."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    result = await show_sync_and_final(
        adapter, _dm_source(), _FINAL_TEXT,
        sleep=_wait_frames_sleep(client, calls, 1))

    assert result is True
    assert calls == [10.0]
    assert _draft_frames(client) == [
        ("thinking", "(Thinking 0s) " + DEMO_REASONING_TOPIC2),
        ("rich", _FINAL_TEXT),
    ]
    # The prime carries the Stop button; the sync draft does not — the
    # turn is over, and the draft must be the plain final chunk so the
    # client adopts the final.
    assert _draft_stop_flags(client) == [True, False]
    sent = _sent_messages(client)
    assert len(sent) == 1
    assert sent[0].message == _FINAL_TEXT
    assert client.requests[-1] is sent[0]
    # The sync draft is the request immediately before the final send.
    assert type(client.requests[-2]).__name__ == "SetTypingRequest"
    sink = next(iter(adapter._streaming_sinks.values()))
    assert sink._finished


@pytest.mark.asyncio
async def test_show_sync_and_final_syncs_first_chunk_of_long_final():
    """show_sync_and_final with a final longer than the draft cap: the
    sync draft is exactly the first chunk (the 4000-char draft cap), not
    the whole final — while the final message itself goes out in full."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []
    long_final = "x" * 5000

    result = await show_sync_and_final(
        adapter, _dm_source(), long_final,
        sleep=_wait_frames_sleep(client, calls, 1))

    assert result is True
    assert calls == [10.0]
    assert _draft_frames(client) == [
        ("thinking", "(Thinking 0s) " + DEMO_REASONING_TOPIC2),
        ("rich", "x" * 4000),
    ]
    assert _draft_stop_flags(client) == [True, False]
    sent = _sent_messages(client)
    assert len(sent) == 1
    assert sent[0].message == long_final
    assert client.requests[-1] is sent[0]


@pytest.mark.asyncio
async def test_show_sync_and_final_returns_false_when_send_fails():
    """show_sync_and_final returns the send's success: False when the
    adapter reports failure. The prime and sync drafts still show, but
    no final message reaches the wire."""
    adapter = _make_adapter()
    client = adapter._client
    seen = []

    async def _failing_send(chat_id, content):
        seen.append((chat_id, content))
        return SimpleNamespace(success=False, error="boom")

    adapter.send = _failing_send

    result = await show_sync_and_final(adapter, _dm_source(), _FINAL_TEXT)

    assert result is False
    assert seen == [("77", _FINAL_TEXT)]
    frames = _draft_frames(client)
    assert frames[2] == ("rich", _FINAL_TEXT)
    # The 10 s prime hold stalls past 3 s with no delta: on_wait
    # fires once and re-asserts the frame, now carrying the ticked
    # thinking seconds — same reasoning, a later prefix.
    assert len(frames) == 3
    assert frames[0] == ("thinking", "(Thinking 0s) " + DEMO_REASONING_TOPIC2)
    assert frames[1][0] == "thinking"
    assert frames[1][1].startswith("(Thinking ")
    assert frames[1][1].endswith(DEMO_REASONING_TOPIC2)
    assert frames[1][1] != frames[0][1]
    assert _sent_messages(client) == []


@pytest.mark.parametrize("call_step", [
    _call_preview, _call_placeholder, _call_reasoning, _call_content,
    _call_sync_and_final,
])
@pytest.mark.asyncio
async def test_steps_return_false_for_non_dm_without_sink(call_step):
    """Every step: non-DM source returns False immediately, no sink
    created, nothing sent, the injected sleep never called."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    async def _never_sleep(delay):
        calls.append(delay)

    assert await call_step(
        adapter, _dm_source(chat_type="group"), _never_sleep) is False
    assert calls == []
    assert client.requests == []
    assert adapter._streaming_sinks == {}


@pytest.mark.parametrize("call_step", [
    _call_preview, _call_placeholder, _call_reasoning, _call_content,
    _call_sync_and_final,
])
@pytest.mark.asyncio
async def test_steps_return_false_without_live_client(call_step):
    """Every step: no live MTProto client returns False with no sink and
    no requests."""
    adapter = _make_adapter()
    adapter._client = None
    calls = []

    async def _never_sleep(delay):
        calls.append(delay)

    assert await call_step(adapter, _dm_source(), _never_sleep) is False
    assert calls == []
    assert adapter._streaming_sinks == {}


@pytest.mark.parametrize("call_step", [
    _call_preview, _call_placeholder, _call_reasoning, _call_content,
])
@pytest.mark.asyncio
async def test_steps_stop_halts_without_final(call_step):
    """Every frame step: a Stop press during the hold halts the step —
    False, no message sent."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    async def _stopping_sleep(delay):
        calls.append(delay)
        await async_wait_until(
            lambda: len(_draft_frames(client)) >= 1,
            what="first draft frame shown")
        for sink in list(adapter._streaming_sinks.values()):
            sink.request_stop()

    assert await call_step(adapter, _dm_source(), _stopping_sleep) is False
    assert _sent_messages(client) == []
    assert {type(r).__name__ for r in client.requests} == {"SetTypingRequest"}


@pytest.mark.asyncio
async def test_show_sync_and_final_stop_before_send_returns_false():
    """show_sync_and_final: a Stop arriving during the sync draft aborts
    before the final send — False, no message."""
    adapter = _make_adapter()
    client = adapter._client
    real_sync = ResponseStreamDraft.finish_sync_draft

    def _stopping_sync(self, final_text):
        self.request_stop()
        return real_sync(self, final_text)

    adapter_send_calls = []
    orig_send = adapter.send

    async def _recording_send(chat_id, content):
        adapter_send_calls.append((chat_id, content))
        return await orig_send(chat_id, content)

    adapter.send = _recording_send
    try:
        ResponseStreamDraft.finish_sync_draft = _stopping_sync
        result = await show_sync_and_final(adapter, _dm_source(), _FINAL_TEXT)
    finally:
        ResponseStreamDraft.finish_sync_draft = real_sync

    assert result is False
    assert adapter_send_calls == []
    assert _sent_messages(client) == []


@pytest.mark.asyncio
async def test_show_sync_and_final_dead_sink_returns_false():
    """show_sync_and_final: a sink that died during the sync draft aborts
    before the final send — False, no message."""
    adapter = _make_adapter()
    client = adapter._client
    real_sync = ResponseStreamDraft.finish_sync_draft

    def _dying_sync(self, final_text):
        self._disabled = True
        return real_sync(self, final_text)

    try:
        ResponseStreamDraft.finish_sync_draft = _dying_sync
        result = await show_sync_and_final(adapter, _dm_source(), _FINAL_TEXT)
    finally:
        ResponseStreamDraft.finish_sync_draft = real_sync

    assert result is False
    assert _sent_messages(client) == []


@pytest.mark.parametrize("call_step", [
    _call_preview, _call_placeholder, _call_reasoning, _call_content,
])
@pytest.mark.asyncio
async def test_steps_unresolvable_peer_returns_false(call_step):
    """Every frame step: sink failure (peer unresolvable, drafts
    disabled) stops the step after the hold — no frames, no final
    message, False."""
    adapter = _make_adapter(peer_cached=False)
    client = adapter._client
    calls = []

    async def _failing_sleep(delay):
        calls.append(delay)
        await async_wait_until(
            lambda: any(getattr(s, "_disabled", False)
                        for s in adapter._streaming_sinks.values()),
            what="demo sink disabled after peer failure")

    assert await call_step(adapter, _dm_source(), _failing_sleep) is False
    assert _draft_frames(client) == []
    assert _sent_messages(client) == []


# --- Full SendMessage*Action enumeration ----------------------------------
# The demo covers EVERY SendMessage*Action the installed Telethon wheel
# knows: the three draft actions via the sink steps, the other fifteen
# via show_action / show_stop_draft. A future Telethon bump that adds
# an action fails the enumeration test until the demo covers it.

def test_enumerated_actions_match_the_wheel():
    """The demo's action catalog is exactly the wheel's SendMessage*Action
    set: 15 raw typing actions plus the 3 draft actions. Every raw
    action builds (progress actions at 50%); the two payload drafts
    need their payload and are built by the sink, never bare."""
    from telethon import types as tl_types

    wheel = {n for n in dir(tl_types)
             if n.startswith("SendMessage") and n.endswith("Action")}
    covered = {name for name, _ in NON_DRAFT_ACTIONS} | set(DRAFT_ACTION_NAMES)
    assert wheel == covered, (
        f"wheel actions not covered by the demo: {sorted(wheel - covered)}; "
        f"demo actions gone from the wheel: {sorted(covered - wheel)}")
    assert len(NON_DRAFT_ACTIONS) == 15
    assert len(DRAFT_ACTION_NAMES) == 3
    for name, _ in NON_DRAFT_ACTIONS:
        assert build_action(name) is not None
    assert build_action("SendMessageStopDraftAction") is not None
    with pytest.raises(ValueError):
        build_action("SendMessageTextDraftAction")
    with pytest.raises(ValueError):
        build_action("SendMessageRichMessageDraftAction")
    with pytest.raises(ValueError):
        build_action("SendMessageNopeAction")
    # Deliberately out of scope — SendMessageAction subtypes that are
    # not typing statuses demoable in a DM: SpeakingInGroupCallAction
    # only means something inside a group call, and the emoji
    # interactions need a target message plus an interaction payload.
    assert "SpeakingInGroupCallAction" in dir(tl_types)
    assert "SendMessageEmojiInteraction" in dir(tl_types)
    assert "SendMessageEmojiInteractionSeen" in dir(tl_types)


# --- Raw typing actions ---------------------------------------------------
# show_action sends one raw (non-draft) typing action via SetTypingRequest,
# re-sent every 4 s so the client keeps showing it for the full 10 s hold
# (one send expires after ~5 s). Never a sent message.

def _action_requests(client):
    """(TL type name, action) per SetTypingRequest, in send order."""
    return [(type(r.action).__name__, r.action) for r in client.requests
            if type(r).__name__ == "SetTypingRequest"]


def _recording_sleep(calls):
    async def _sleep(delay):
        calls.append(delay)
    return _sleep


@pytest.mark.asyncio
async def test_show_action_typing_resent_over_ten_second_hold():
    """show_action with typing: the action goes out 3 times over the
    10 s hold (sleeps 4 + 4 + 2), and no message is ever sent."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    result = await show_action(
        adapter, _dm_source(), "SendMessageTypingAction",
        sleep=_recording_sleep(calls))

    assert result is True
    assert calls == [4.0, 4.0, 2.0]
    assert sum(calls) >= 10.0  # the animation holds 10 s or more
    assert [name for name, _ in _action_requests(client)] == [
        "SendMessageTypingAction"] * 3  # re-sent, not a single send
    assert _sent_messages(client) == []
    assert _draft_frames(client) == []  # a raw action, never a draft frame
    sink = next(iter(adapter._streaming_sinks.values()))
    assert sink._finished


@pytest.mark.asyncio
async def test_show_action_upload_photo_carries_progress_50():
    """Upload actions carry progress=50 on every re-send."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    result = await show_action(
        adapter, _dm_source(), "SendMessageUploadPhotoAction",
        sleep=_recording_sleep(calls))

    assert result is True
    assert calls == [4.0, 4.0, 2.0]
    actions = _action_requests(client)
    assert len(actions) == 3
    assert all(name == "SendMessageUploadPhotoAction" for name, _ in actions)
    assert all(action.progress == 50 for _, action in actions)
    assert _sent_messages(client) == []


@pytest.mark.parametrize("action_name", [name for name, _ in NON_DRAFT_ACTIONS])
@pytest.mark.asyncio
async def test_show_action_covers_every_raw_action(action_name):
    """Every enumerated raw action: demoed for the full 10 s hold,
    re-sent more than once, never a sent message."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    result = await show_action(
        adapter, _dm_source(), action_name,
        sleep=_recording_sleep(calls))

    assert result is True
    assert sum(calls) >= 10.0
    actions = _action_requests(client)
    assert len(actions) > 1  # re-sent inside the hold
    assert all(name == action_name for name, _ in actions)
    assert _sent_messages(client) == []


@pytest.mark.asyncio
async def test_show_action_returns_false_for_non_dm_without_sink():
    """Non-DM source: False immediately, no sink created, nothing sent,
    the injected sleep never called."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    assert await show_action(
        adapter, _dm_source(chat_type="group"), "SendMessageTypingAction",
        sleep=_recording_sleep(calls)) is False
    assert calls == []
    assert client.requests == []
    assert adapter._streaming_sinks == {}


@pytest.mark.asyncio
async def test_show_action_returns_false_without_live_client():
    """No live MTProto client: False with no sink and no requests."""
    adapter = _make_adapter()
    adapter._client = None
    calls = []

    assert await show_action(
        adapter, _dm_source(), "SendMessageTypingAction",
        sleep=_recording_sleep(calls)) is False
    assert calls == []
    assert adapter._streaming_sinks == {}


@pytest.mark.asyncio
async def test_show_action_unresolvable_peer_returns_false():
    """Sink failure (peer unresolvable): False, no typing action sent."""
    adapter = _make_adapter(peer_cached=False)
    client = adapter._client
    calls = []

    assert await show_action(
        adapter, _dm_source(), "SendMessageTypingAction",
        sleep=_recording_sleep(calls)) is False
    assert _action_requests(client) == []
    assert _sent_messages(client) == []


@pytest.mark.asyncio
async def test_show_action_send_failure_returns_false():
    """A rejected action (client raises) fails the step cleanly: False,
    no message, and the step never breaks its caller."""
    adapter = _make_adapter()

    class _RaisingClient:
        async def __call__(self, request):
            raise RuntimeError("boom")

    adapter._client = _RaisingClient()
    calls = []

    assert await show_action(
        adapter, _dm_source(), "SendMessageTypingAction",
        sleep=_recording_sleep(calls)) is False
    assert calls == []


@pytest.mark.asyncio
async def test_show_action_stop_halts_without_message():
    """A Stop press during the hold halts the step: False, no message —
    and no persist message is owed for the stopped animation."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    async def _stopping_sleep(delay):
        calls.append(delay)
        for sink in list(adapter._streaming_sinks.values()):
            sink.request_stop()

    assert await show_action(
        adapter, _dm_source(), "SendMessageTypingAction",
        sleep=_stopping_sleep) is False
    assert calls == [4.0]  # stopped after the first re-send interval
    assert len(_action_requests(client)) == 1  # the first send landed
    assert _sent_messages(client) == []


@pytest.mark.asyncio
async def test_show_action_unknown_name_returns_false():
    """An action name the wheel does not know fails the step cleanly."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    assert await show_action(
        adapter, _dm_source(), "SendMessageNopeAction",
        sleep=_recording_sleep(calls)) is False
    assert calls == []
    assert client.requests == []
    assert adapter._streaming_sinks == {}


# --- Stop-draft action ----------------------------------------------------
# show_stop_draft shows the demo reasoning draft, then clears it with
# SendMessageStopDraftAction naming this sink's random_id, re-sent so a
# late pump frame cannot resurrect the draft inside the hold.

@pytest.mark.asyncio
async def test_show_stop_draft_shows_reasoning_then_clears_it():
    """show_stop_draft: the reasoning draft shows, then the stop action
    (this sink's random_id) clears it — twice over the 10 s hold."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    async def _sleep(delay):
        calls.append(delay)
        await async_wait_until(
            lambda: len(_draft_frames(client)) >= 1,
            what="reasoning draft shown")

    result = await show_stop_draft(adapter, _dm_source(), sleep=_sleep)

    assert result is True
    assert calls == [4.0, 4.0, 2.0]
    assert sum(calls) >= 10.0
    assert _draft_frames(client) == [("thinking", "(Thinking 0s) " + DEMO_REASONING_TOPIC2)]
    actions = _action_requests(client)
    # One reasoning draft frame, then two stop actions — no other frame.
    assert len(actions) == 3
    assert actions[0][0] == "InputSendMessageRichMessageDraftAction"
    assert all(name == "SendMessageStopDraftAction" for name, _ in actions[1:])
    sink = next(iter(adapter._streaming_sinks.values()))
    assert all(action.random_id == sink.random_id for _, action in actions[1:])
    assert _sent_messages(client) == []
    assert sink._finished


@pytest.mark.asyncio
async def test_show_stop_draft_returns_false_for_non_dm():
    """Non-DM source: False immediately, nothing sent, no sink."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    assert await show_stop_draft(
        adapter, _dm_source(chat_type="group"),
        sleep=_recording_sleep(calls)) is False
    assert calls == []
    assert client.requests == []
    assert adapter._streaming_sinks == {}


@pytest.mark.asyncio
async def test_show_stop_draft_stop_halts_without_message():
    """A Stop press while the reasoning draft shows halts the step:
    False, the stop action never goes out, no message."""
    adapter = _make_adapter()
    client = adapter._client
    calls = []

    async def _stopping_sleep(delay):
        calls.append(delay)
        await async_wait_until(
            lambda: len(_draft_frames(client)) >= 1,
            what="reasoning draft shown")
        for sink in list(adapter._streaming_sinks.values()):
            sink.request_stop()

    assert await show_stop_draft(adapter, _dm_source(), sleep=_stopping_sleep) is False
    assert calls == [4.0]
    assert _draft_frames(client) == [("thinking", "(Thinking 0s) " + DEMO_REASONING_TOPIC2)]
    actions = _action_requests(client)
    assert len(actions) == 1  # only the draft frame, no stop action
    assert actions[0][0] == "InputSendMessageRichMessageDraftAction"
    assert _sent_messages(client) == []


# --- Persist messages -----------------------------------------------------
# send_animation_message sends one animation's persist message as plain
# text through the adapter's plain send — the same path as the demo
# final message. DM-only, live-client guard, True on success.

class _SendStub:
    """Adapter stub with a controllable plain send."""

    def __init__(self, result=None, *, fail=False):
        self._client = object()
        self.result = result
        self.fail = fail
        self.calls = []

    async def send(self, chat_id, text, **kwargs):
        self.calls.append((chat_id, text))
        if self.fail:
            raise RuntimeError("boom")
        return self.result


@pytest.mark.asyncio
async def test_send_animation_message_sends_exact_text():
    """The persist message goes out via adapter.send with the exact
    text, to the DM chat, and returns True."""
    stub = _SendStub(SimpleNamespace(success=True))

    assert await send_animation_message(
        stub, _dm_source(), "Placeholder\nThinking") is True
    assert stub.calls == [("77", "Placeholder\nThinking")]


@pytest.mark.asyncio
async def test_send_animation_message_returns_false_when_send_fails():
    """A failed send (success=False) returns False — the handler aborts
    the rest of the sequence instead of continuing past a lost message."""
    stub = _SendStub(SimpleNamespace(success=False, error="boom"))

    assert await send_animation_message(
        stub, _dm_source(), "Reasoning\ndemo") is False
    assert stub.calls == [("77", "Reasoning\ndemo")]


@pytest.mark.asyncio
async def test_send_animation_message_returns_false_when_send_raises():
    """A raising send returns False, never an exception to the caller."""
    stub = _SendStub(fail=True)

    assert await send_animation_message(
        stub, _dm_source(), "Reasoning\ndemo") is False
    assert stub.calls == [("77", "Reasoning\ndemo")]


@pytest.mark.asyncio
async def test_send_animation_message_returns_false_for_non_dm():
    """Non-DM source: False, nothing sent."""
    stub = _SendStub(SimpleNamespace(success=True))

    assert await send_animation_message(
        stub, _dm_source(chat_type="group"), "Typing\n...") is False
    assert stub.calls == []


@pytest.mark.asyncio
async def test_send_animation_message_returns_false_without_live_client():
    """No live client: False, nothing sent."""
    stub = _SendStub(SimpleNamespace(success=True))
    stub._client = None

    assert await send_animation_message(
        stub, _dm_source(), "Typing\n...") is False
    assert stub.calls == []
