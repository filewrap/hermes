"""Streaming response drafts (Beta): rich live drafts for Telegram DM turns.

The draft is an ephemeral rich preview (Thinking block while reasoning-only,
Markdown once content flows); the complete final message still goes out once
through the normal delivery path. Groups get normal replies, no edit-loop
anywhere, and every draft failure fails open.
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest
from telethon import errors, functions, types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.streaming_response import (
    ResponseStreamDraft,
    persist_streaming_response_setting,
    streaming_response_enabled,
)
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from tests.waiting import async_wait_until


class _DraftClient:
    """Fake Telethon client: ``requests`` is every call, ``sent`` only the ones that did not raise."""

    def __init__(self, *, fail_drafts=None, users=None):
        self.requests = []
        self.sent = []
        self.fail_drafts = fail_drafts
        self.users = users

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.users.GetUsersRequest):
            return list(self.users or [])
        if type(request).__name__ == "SetTypingRequest" and self.fail_drafts is not None:
            error = self.fail_drafts(request)
            if error is not None:
                raise error
        self.sent.append(request)
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
    """(kind, payload, random_id, thinking) per successfully sent draft, in order.

    ``thinking`` is the Thinking-block text carried by the frame, or None:
    a thinking-only frame, and a rich frame once content flows (the frozen
    reasoning stays visible above the content)."""
    frames = []
    for request in client.sent:
        if type(request).__name__ != "SetTypingRequest":
            continue
        action = request.action
        assert request.top_msg_id is None
        if type(action).__name__ in ("SendMessageTypingAction",
                                     "SendMessageCancelAction"):
            # Plain typing actions and the post-final clear carry no
            # draft preview payload.
            continue
        if type(action).__name__ == "InputSendMessageRichMessageDraftAction":
            rich = action.rich_message
            if type(rich).__name__ == "InputRichMessage":
                blocks = list(rich.blocks)
                thinking = None
                if blocks and type(blocks[0]).__name__ == "PageBlockThinking":
                    thinking = _rich_text(blocks[0].text)
                if len(blocks) == 1 and thinking is not None:
                    frames.append(("thinking", thinking, action.random_id, thinking))
                else:
                    frames.append(("rich", _blocks_text(blocks), action.random_id, thinking))
            else:
                assert type(rich).__name__ == "InputRichMessageMarkdown"
                frames.append(("rich", rich.markdown, action.random_id, None))
        else:
            assert type(action).__name__ == "SendMessageTextDraftAction"
            frames.append(("text", action.text.text, action.random_id, None))
    return frames


def _set_typing_requests(client):
    return [r for r in client.requests if type(r).__name__ == "SetTypingRequest"]


def _typing_actions(client):
    """Plain ``SendMessageTypingAction`` requests that landed: the header
    typing indicator, as distinct from draft actions (the bubble)."""
    return [r for r in client.sent
            if type(r).__name__ == "SetTypingRequest"
            and type(r.action).__name__ == "SendMessageTypingAction"]


def _fake_adapter(client, *, peer=None, extra=None, peer_cache=None):
    adapter = SimpleNamespace(_client=client)
    adapter.config = PlatformConfig(extra=extra or {})
    adapter._truthy_setting = TelethonAdapter._truthy_setting
    adapter._peer_cache = peer_cache if peer_cache is not None else {}

    async def _input_peer_for(chat_id):
        return peer if peer is not None else types.InputPeerUser(int(chat_id), 123)

    adapter._input_peer_for = _input_peer_for

    def _remember_peer(chat_id, resolved):
        adapter._peer_cache[str(chat_id)] = resolved

    adapter._remember_peer = _remember_peer
    return adapter


def _dm_source(chat_id="77", thread_id=None):
    return SimpleNamespace(chat_id=chat_id, thread_id=thread_id, chat_type="dm")


def test_streaming_response_defaults_off_and_extra_enables():
    adapter = _fake_adapter(_DraftClient())
    assert streaming_response_enabled(adapter) is False

    adapter = _fake_adapter(_DraftClient(), extra={"streaming_response": True})
    assert streaming_response_enabled(adapter) is True

    adapter._streaming_response_runtime = False
    assert streaming_response_enabled(adapter) is False


def test_persist_streaming_response_setting_writes_config_and_extra(tmp_path):
    client = _DraftClient()
    adapter = _fake_adapter(client)
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path)
    source = _dm_source()

    persist_streaming_response_setting(adapter, source, True)

    from hermes_cli.config import read_user_config_raw

    assert read_user_config_raw(tmp_path / "config.yaml")["platforms"]["telegram"]["streaming_response"] is True
    assert adapter.config.extra["streaming_response"] is True
    assert adapter._streaming_response_runtime is True
    assert streaming_response_enabled(adapter) is True


def test_streaming_response_sink_factory_gates_groups_guests_and_setting_off():
    client = _DraftClient()

    def _sink(source, extra):
        adapter = TelethonAdapter(PlatformConfig(extra=extra))
        adapter._client = client
        return adapter.streaming_response_sink(source, None)

    assert _sink(SimpleNamespace(chat_id="77", thread_id=None, chat_type="group"),
                 {"streaming_response": True}) is None
    assert _sink(SimpleNamespace(chat_id="77", thread_id="guest-77", chat_type="dm"),
                 {"streaming_response": True}) is None
    assert _sink(_dm_source(), {}) is None
    sink = _sink(_dm_source(), {"streaming_response": True})
    assert isinstance(sink, ResponseStreamDraft)


@pytest.mark.asyncio
async def test_draft_shows_reasoning_as_thinking_block_then_rich_content():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_reasoning("Thinking through it")
        sink.on_content("Hello")
        sink.on_content(" world")
        await async_wait_until(lambda: len(_draft_frames(client)) == 3)

        frames = _draft_frames(client)
        assert frames[0][:2] == ("thinking", "(Thinking 0s) Thinking through it")
        assert frames[1][:2] == ("rich", "Hello")
        assert frames[2][:2] == ("rich", "Hello world")
        # The frozen reasoning stays visible above the content once it
        # flows — it must not vanish mid-stream.
        assert frames[1][3] == "Thinking through it"
        assert frames[2][3] == "Thinking through it"
        # One fixed random_id per generation: the same id animates the draft.
        assert len({frame[2] for frame in frames}) == 1
        assert frames[0][2] != 0
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_draft_reasoning_renders_italic_in_thinking_block():
    """The draft's Thinking block carries its reasoning in italic — the
    draft-only formation; the final message's Reasoning section stays a
    code block. Asserted on the rendered rich node, not on source text."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_reasoning("Thinking through it")
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)

        thinking_blocks = []
        for request in client.sent:
            if type(request).__name__ != "SetTypingRequest":
                continue
            action = request.action
            if type(action).__name__ != "InputSendMessageRichMessageDraftAction":
                continue
            rich = action.rich_message
            if type(rich).__name__ != "InputRichMessage":
                continue
            thinking_blocks.extend(
                block for block in rich.blocks
                if type(block).__name__ == "PageBlockThinking")
        # Thinking-only frame + frozen reasoning above the content frame.
        # The "(Thinking Ns)" prefix rides inside the italic Thinking
        # block of the reasoning-only frame; the frozen reasoning above
        # the content carries none — content has arrived.
        assert len(thinking_blocks) == 2
        for block in thinking_blocks:
            assert type(block.text).__name__ == "TextItalic"
            assert type(block.text.text).__name__ == "TextPlain"
        assert thinking_blocks[0].text.text.text == "(Thinking 0s) Thinking through it"
        assert thinking_blocks[1].text.text.text == "Thinking through it"
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_placeholder_draft_is_plain_text_draft_not_a_sent_message():
    """The "Thinking" gap cover is a draft action only: a plain-text
    draft via SetTypingRequest, never a sent message — no SendMessageRequest
    is ever issued for it, and nothing persists in the chat."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.prime_thinking_placeholder()
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        assert _draft_frames(client) == [("text", "Thinking", sink.random_id, None)]
        assert {type(r).__name__ for r in client.requests} == {"SetTypingRequest"}
        action = _set_typing_requests(client)[0].action
        assert type(action).__name__ == "SendMessageTextDraftAction"
        assert action.text.text == "Thinking"
        # The Stop button is live during the gap: the placeholder carries
        # the sink's fixed random_id, so a Stop press routes to this sink.
        assert action.can_stop is True
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_placeholder_replaced_by_first_reasoning_delta():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.prime_thinking_placeholder()
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        sink.on_reasoning("Thinking through it")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        frames = _draft_frames(client)
        assert frames[0][:2] == ("text", "Thinking")
        assert frames[1][:2] == ("thinking", "(Thinking 0s) Thinking through it")
        # One fixed random_id per generation: the placeholder and the
        # reasoning draft are the same live draft, replaced in place.
        assert frames[0][2] == frames[1][2] == sink.random_id
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_placeholder_replaced_by_first_content_delta():
    """With Display off no reasoning ever arrives; the first content draft
    still replaces the placeholder — it never survives past real content."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.prime_thinking_placeholder()
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert [(kind, text) for kind, text, _, _ in _draft_frames(client)] == [
            ("text", "Thinking"), ("rich", "Hello"),
        ]
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_placeholder_keepalive_repushes_until_reasoning_arrives():
    """The pump re-pushes the placeholder inside the ~30 s draft TTL while
    the turn is still thinking, so the gap cover does not die early."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop,
                               min_interval_s=0, keepalive_s=0.2)
    try:
        sink.prime_thinking_placeholder()
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert [(kind, text) for kind, text, _, _ in _draft_frames(client)] == [
            ("text", "Thinking"), ("text", "Thinking"),
        ]
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_placeholder_stays_static_in_load_lane():
    """The LOAD lane no longer cycles the placeholder: the draft is primed
    once as static "Thinking" and never re-sent with different text — the
    ••• typing indicator carries all the motion."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.prime_thinking_placeholder()
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        started = loop.time()
        # Past two old 1 s animation intervals: on the old code the pump
        # would have cycled through "Thinking." / "Thinking.." by now.
        await async_wait_until(lambda: loop.time() - started >= 2.5,
                               what="waited past two old animation intervals")
        assert {text for _kind, text, _, _ in _draft_frames(client)} == {"Thinking"}
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_prime_fires_on_load_once():
    """prime_thinking_placeholder fires on_load once and enters the LOAD
    lane; a second prime is a no-op and fires nothing again."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    loads = []
    sink.on_load = lambda: loads.append(True)
    try:
        sink.prime_thinking_placeholder()
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        sink.prime_thinking_placeholder()
        assert loads == [True]
        assert sink._lane == "load"
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_thinking_frame_keeps_growing_past_old_2000_cap():
    """The Thinking draft is a preview: 5,000 chars of single-line
    reasoning keep animating the frame instead of freezing at the old
    2000-char cap while the accumulation grows underneath. One line
    with no newlines is a single window line, so the 7-line window is
    a no-op here — the window only cuts across newline-separated
    lines. The frame text is the "(Thinking Ns)" prefix plus the
    reasoning, so it runs 14 chars longer than the reasoning itself."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        for _ in range(10):
            sink.on_reasoning("x" * 500)
        await async_wait_until(lambda: len(_draft_frames(client)) == 10)
        frames = _draft_frames(client)
        assert len(frames[-1][1]) == 5014
        assert frames[-1][1] == "(Thinking 0s) " + "x" * 5000
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_combined_thinking_plus_content_stays_under_rich_ceiling():
    """Raising the thinking cap must not blow the rich ceiling: 8000 chars
    of frozen reasoning plus 4000 chars of content land at 12,000, well
    under the 32,768 rich payload cap."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_reasoning("y" * 8000)
        sink.on_content("z" * 4000)
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        kind, content, _rid, thinking = _draft_frames(client)[-1]
        assert kind == "rich"
        assert thinking == "y" * 8000
        assert content == "z" * 4000
        assert len(thinking) + len(content) == 12000
        assert len(thinking) + len(content) <= 32768
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_draft_content_past_limit_keeps_newest_and_drops_oldest():
    """The user's order: once the draft exceeds _DRAFT_MAX_CHARS, the oldest
    chunks go and the draft shows the newest content — it must not freeze on
    the first 4000 chars."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        early, late = "A" + "x" * 2999, "B" * 3000
        sink.on_content(early)
        sink.on_content(late)
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)

        frames = _draft_frames(client)
        assert frames[0][:2] == ("rich", early)
        assert frames[1][1] == (early + late)[-4000:]
        assert "A" not in frames[1][1]
        assert frames[1][1].endswith("B" * 3000)
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_draft_content_under_limit_is_untouched():
    """Below the cap nothing is trimmed: the draft is the accumulated content."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_content("Hello")
        sink.on_content(" world")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert _draft_frames(client)[1][:2] == ("rich", "Hello world")
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_draft_reasoning_past_cap_keeps_newest_and_drops_oldest():
    """Same tail rule for the reasoning frame: past 8000 chars the draft
    shows the newest thinking, not the first 8000."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        early, late = "R" + "y" * 5999, "S" * 4000
        sink.on_reasoning(early)
        sink.on_reasoning(late)
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)

        frames = _draft_frames(client)
        assert frames[0][:2] == ("thinking", "(Thinking 0s) " + early)
        assert frames[1][1] == "(Thinking 0s) " + (early + late)[-8000:]
        assert "R" not in frames[1][1]
        assert frames[1][1].endswith("S" * 4000)
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_finish_sync_draft_with_trimmed_draft_sends_first_chunk():
    """A tail-windowed last draft never shares its leading chars with the
    final, so the sync draft is always sent — and it must be the FIRST chunk
    of the final (a prefix), because the client adopts the final only
    against a leading prefix of the last draft."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        early, late = "A" + "x" * 2999, "B" * 3000
        final = early + late
        sink.on_content(early)
        sink.on_content(late)
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert _draft_frames(client)[1][1] == final[-4000:]

        await asyncio.to_thread(sink.finish_sync_draft, final)
        await async_wait_until(lambda: len(_draft_frames(client)) == 3)
        # The sync draft is the first chunk of the final, not the tail.
        assert _draft_frames(client)[2][:2] == ("rich", final[:4000])
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_clear_draft_sends_cancel_action_after_final():
    """After the final lands, the sink clears the live draft with one
    explicit CancelAction on its resolved peer — the Stop button and the
    animating send icon must end at once, not linger to the TTL."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        assert await sink.clear_draft() is True
        cancels = [r for r in client.requests
                   if type(r).__name__ == "SetTypingRequest"
                   and type(r.action).__name__ == "SendMessageCancelAction"]
        assert len(cancels) == 1
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_clear_draft_is_noop_without_a_shown_draft():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        assert await sink.clear_draft() is False
        assert client.requests == []
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_request_stop_fires_on_cancel_once(caplog):
    """A routed Stop press fires on_cancel (in addition to on_stop) and
    logs at INFO — host logs must prove whether Telegram delivered the
    Stop update at all. A second Stop is a no-op."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    cancelled = []
    sink.on_cancel = lambda: cancelled.append(True)
    try:
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        with caplog.at_level(
                logging.INFO, logger="plugins.platforms.telegram.streaming_response"):
            sink.request_stop()
        assert cancelled == [True]
        assert sink._lane == "cancelled"
        assert "Stop press routed" in caplog.text
        sink.request_stop()
        assert cancelled == [True]
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_finish_sync_draft_fires_on_end_once():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    ended = []
    sink.on_end = lambda final, stopped: ended.append((final, stopped))
    try:
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        await asyncio.to_thread(sink.finish_sync_draft, "Hello, done.")
        assert ended == [("Hello, done.", False)]
        assert sink._lane == "end"
        sink.cancel()  # teardown after the end fires nothing again
        assert ended == [("Hello, done.", False)]
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_cancel_fires_on_cancel_and_on_end_once():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    cancelled = []
    ended = []
    sink.on_cancel = lambda: cancelled.append(True)
    sink.on_end = lambda final, stopped: ended.append((final, stopped))
    try:
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        sink.cancel()
        assert cancelled == [True]
        assert ended == [(None, False)]
        assert sink._lane == "cancelled"
        sink.cancel()
        assert cancelled == [True]
        assert ended == [(None, False)]
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_partial_text_preserves_reasoning_when_stopped_during_reasoning():
    """A stop during reasoning must not deliver nothing: the frozen
    reasoning is the partial, rendered as the final's Reasoning code
    block. With content, the partial is the content only — the reasoning
    was already shown in the draft, shown once."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_reasoning("Thinking through it")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        assert sink.partial_text == "**Reasoning:**\n```\nThinking through it\n```"
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert sink.partial_text == "Hello"
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_finish_sync_draft_syncs_when_only_first_char_matches():
    """The old one-char skip rule mistook the placeholder for the final:
    a final starting with the same letter ('Thanks...') shares its first
    char with the 'Thinking' placeholder and skipped the sync draft,
    leaving the placeholder as the last draft. The skip now needs an
    8-char prefix."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.prime_thinking_placeholder()
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        assert _draft_frames(client)[0][:2] == ("text", "Thinking")
        await asyncio.to_thread(sink.finish_sync_draft, "Thanks, got it.")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert _draft_frames(client)[1][:2] == ("rich", "Thanks, got it.")
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_wait_fires_once_per_stall_and_reasserts_frame():
    """A 3 s stall with no delta (reasoning/content lane) fires on_wait
    once and re-asserts the frame, so the draft bubble visibly
    refreshes after any overwrite. The re-assert carries the current
    "(Thinking Ns)" value — same reasoning, same random_id, so the
    client adopts it in place — and N advances with the stall (0s on
    the first frame, 4s on the re-assert), so the two frames are not
    byte-identical."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    clock = [100.0]
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop,
                               min_interval_s=0, keepalive_s=20.0,
                               time_fn=lambda: clock[0])
    waits = []
    sink.on_wait = lambda elapsed: waits.append(elapsed)
    try:
        sink.on_reasoning("Thinking through it")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        # No delta for 4 s of sink time: one wait, one re-assert with N
        # advanced to 4s. Wait for the re-asserted FRAME, not just the
        # hook: the hook fires just before the send, so the hook alone
        # does not prove the frame landed.
        clock[0] = 104.0
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert waits == [pytest.approx(4.0)]
        frames = _draft_frames(client)
        assert frames[0][:2] == ("thinking", "(Thinking 0s) Thinking through it")
        assert frames[1][:2] == ("thinking", "(Thinking 4s) Thinking through it")
        assert frames[0][2] == frames[1][2]
        # Still stalled with the clock far ahead: the pump ticks (0.5 s)
        # but no second wait fires for the same stall. The 20 s keepalive
        # re-sends the frame at 124 s with N advanced to 24s — that is
        # the pump-tick proof — and no plain typing action ever goes out.
        clock[0] = 124.0
        await async_wait_until(lambda: len(_draft_frames(client)) == 3)
        assert waits == [pytest.approx(4.0)]
        assert _typing_actions(client) == []
        assert _draft_frames(client)[2][:2] == (
            "thinking", "(Thinking 24s) Thinking through it")
        assert _draft_frames(client)[2][2] == _draft_frames(client)[0][2]
        # The next delta clears the stall: a fresh stall fires again.
        frames_before = len(_draft_frames(client))
        sink.on_reasoning(" Still thinking.")
        await async_wait_until(lambda: len(_draft_frames(client)) == frames_before + 1)
        clock[0] = 128.0
        await async_wait_until(lambda: len(waits) == 2)
        assert waits == [pytest.approx(4.0), pytest.approx(4.0)]
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_no_plain_typing_action_during_long_delta_gap():
    """The typing heartbeat is gone: during a long delta gap the sink
    sends only draft frames — no plain SendMessageTypingAction ever goes
    out, even past the old 4 s gap with the throttle gate open. The
    draft bubble stays one continuous draft; the header typing indicator
    lapsing mid-turn is the accepted trade-off."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    clock = [100.0]
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop,
                               min_interval_s=1.0, keepalive_s=3600,
                               time_fn=lambda: clock[0])
    waits = []
    sink.on_wait = lambda elapsed: waits.append(elapsed)
    try:
        sink.on_reasoning("Thinking through it")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        # First stall at 104 s: wait fires, identical frame re-asserted.
        clock[0] = 104.0
        await async_wait_until(lambda: len(waits) == 1)
        assert waits == [pytest.approx(4.0)]
        # 105 s: past the old 4 s gap with the throttle gate open (last
        # send was the 104 s re-assert) — on the old code the heartbeat
        # fired a plain typing action on the next pump tick. Give the
        # pump a full 2 s window to prove none comes.
        clock[0] = 105.0
        with pytest.raises(AssertionError):
            await async_wait_until(lambda: len(_typing_actions(client)) >= 1,
                                   timeout=2.0)
        assert waits == [pytest.approx(4.0)]
        assert _typing_actions(client) == []
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_draft_freezes_reasoning_once_content_starts():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_reasoning("r1")
        sink.on_content("c1")
        sink.on_reasoning("r2")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)

        assert [(kind, text) for kind, text, _, _ in _draft_frames(client)] == [
            ("thinking", "(Thinking 0s) r1"), ("rich", "c1"),
        ]
        # Later reasoning is dropped (frozen), but the frozen reasoning
        # stays visible in the rich frame.
        assert _draft_frames(client)[1][3] == "r1"
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_first_content_frame_shifts_lane_immediately_and_fires_hook():
    """REASONING -> CONTENT is a visible lane shift, not a throttle wait:
    the first content frame goes out at once (bypassing the 1 s gate)
    and on_lane_shift fires before it is sent."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    clock = [100.0]
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop,
                               min_interval_s=1.0, time_fn=lambda: clock[0])
    shifts = []
    sink.on_lane_shift = lambda before, after: shifts.append((before, after))
    try:
        sink.on_reasoning("r")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        # The gate is shut (frozen clock, last send at 100.0) — a
        # trailing-edge send would hold this frame, not deliver it.
        sink.on_content("c")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert shifts == [("reasoning", "content")]
        assert sink._lane == "content"
        assert [(kind, text) for kind, text, _, _ in _draft_frames(client)] == [
            ("thinking", "(Thinking 0s) r"), ("rich", "c"),
        ]
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_build_action_failure_keeps_frame_pending():
    """A frame whose action cannot be built is not silently dropped: it
    stays pending for the pump to retry, and counts as a failure like
    any other transient send failure."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    clock = [100.0]
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop,
                               min_interval_s=1.0, time_fn=lambda: clock[0])
    original_build = sink._build_action
    calls = []

    def _flaky_build(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return original_build(*args, **kwargs)

    sink._build_action = _flaky_build
    try:
        sink.on_content("Hello")
        # The first send attempt ran and failed on the build.
        await async_wait_until(lambda: sink._consecutive_failures == 1)
        assert sink._pending is True
        # Nothing was dropped silently: no frame landed, but the frame
        # is still owed to the pump (frozen clock, so no retry fires here).
        assert _draft_frames(client) == []
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_draft_throttles_with_trailing_edge():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    clock = [100.0]
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop,
                               min_interval_s=1.0, time_fn=lambda: clock[0])
    try:
        sink.on_reasoning("r")
        sink.on_content("a")
        sink.on_content("b")
        # The first content frame leaves the reasoning lane immediately
        # (lane shift), so both lane-entry frames land at once; only the
        # later deltas coalesce behind the gate.
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert [(kind, text) for kind, text, _, _ in _draft_frames(client)] == [
            ("thinking", "(Thinking 0s) r"), ("rich", "a"),
        ]

        # The held deltas coalesce: the next frame out carries the latest text,
        # whether the gate opens on a new delta or on the pump's flush.
        clock[0] = 102.0
        sink.on_content("c")
        await async_wait_until(lambda: len(_draft_frames(client)) >= 3)
        assert _draft_frames(client)[-1][:2] == ("rich", "abc")
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_rich_rejection_falls_back_to_text_draft():
    def _fail_rich(request):
        if type(request.action).__name__ == "InputSendMessageRichMessageDraftAction":
            return errors.BadRequestError(request, "RICH_MESSAGE_EMPTY")
        return None

    client = _DraftClient(fail_drafts=_fail_rich)
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        assert _draft_frames(client)[0][:2] == ("text", "Hello")

        sink.on_content(" world")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert _draft_frames(client)[1][:2] == ("text", "Hello world")
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_peer_invalid_re_resolves_fresh_and_retries_once():
    stale = types.InputPeerUser(77, 111)
    fresh = types.InputPeerUser(77, 999)
    peers = [stale, fresh]
    failed_once = [False]

    def _fail_once(request):
        if not failed_once[0]:
            failed_once[0] = True
            return errors.PeerIdInvalidError(request)
        return None

    client = _DraftClient(fail_drafts=_fail_once)
    adapter = _fake_adapter(client, peer_cache={"77": stale})

    async def _input_peer_for(chat_id):
        return peers.pop(0)

    adapter._input_peer_for = _input_peer_for
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(adapter, "77", loop=loop, min_interval_s=0)
    try:
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        assert _draft_frames(client)[0][:2] == ("rich", "Hello")
        # The stale peer was attempted once, then the fresh one.
        assert [r.peer for r in _set_typing_requests(client)] == [stale, fresh]
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_unresolvable_peer_disables_sink_without_draft():
    async def _never_resolves(chat_id):
        raise ValueError("Could not find the input entity")

    client = _DraftClient(users=[types.UserEmpty(id=77)])
    adapter = _fake_adapter(client)
    adapter._input_peer_for = _never_resolves
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(adapter, "77", loop=loop, min_interval_s=0)
    try:
        sink.on_content("Hello")
        sink.on_content(" world")
        await async_wait_until(lambda: sink._disabled)
        assert _set_typing_requests(client) == []
        assert _draft_frames(client) == []
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_textdraft_peer_invalid_disables_sink():
    def _fail(request):
        return errors.BadRequestError(request, "TEXTDRAFT_PEER_INVALID")

    client = _DraftClient(fail_drafts=_fail)
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_content("Hello")
        await async_wait_until(lambda: sink._disabled)
        sink.on_content(" world")
        assert len(_set_typing_requests(client)) == 1
        assert _draft_frames(client) == []
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_keepalive_repushes_current_frame():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop,
                               min_interval_s=0, keepalive_s=0.2)
    try:
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        frames = _draft_frames(client)
        assert frames[0][:2] == ("rich", "Hello")
        assert frames[1][:2] == ("rich", "Hello")
        assert frames[0][2] == frames[1][2]
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_finish_sync_draft_sends_first_chunk_when_leading_char_differs():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_reasoning("Thinking through it")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        assert _draft_frames(client)[0][:2] == ("thinking", "(Thinking 0s) Thinking through it")

        await asyncio.to_thread(
            sink.finish_sync_draft, "Hello world, this is the answer.")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)
        assert _draft_frames(client)[1][:2] == ("rich", "Hello world, this is the answer.")

        # Finished: later deltas never produce a ghost draft.
        sink.on_content("more")
        assert len(_draft_frames(client)) == 2
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_finish_sync_draft_skips_when_last_frame_shares_leading_char():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_content("Hello world")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        assert _draft_frames(client)[0][:2] == ("rich", "Hello world")

        await asyncio.to_thread(
            sink.finish_sync_draft, "Hello world, continued.")
        assert len(_draft_frames(client)) == 1
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_cancel_stops_keepalive():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop,
                               min_interval_s=0, keepalive_s=0.2)
    try:
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        sink.cancel()
        assert len(_draft_frames(client)) == 1
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_draft_actions_carry_stop_button():
    """Every live draft action shows the native Stop button (can_stop) and
    keeps the partial visible after Stop (keep_on_stop); the sync draft —
    sent when the turn is over — carries neither."""
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_reasoning("Thinking through it")
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 2)

        actions = [r.action for r in _set_typing_requests(client)]
        assert len(actions) == 2
        for action in actions:
            assert action.can_stop is True
            assert action.keep_on_stop is True

        await asyncio.to_thread(sink.finish_sync_draft, "Hello, done.")
        sync = [r.action for r in _set_typing_requests(client)][-1]
        assert sync.can_stop in (False, None)
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_request_stop_halts_sink_and_fires_on_stop_once():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    stopped = []
    sink.on_stop = lambda: stopped.append(True)
    try:
        sink.on_content("Hello")
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        sink.request_stop()
        assert sink.stop_requested
        assert sink._finished
        assert len(stopped) == 1
        # A second Stop is a no-op: the turn is already halted.
        sink.request_stop()
        assert len(stopped) == 1
        # No further draft goes out after Stop.
        sink.on_content(" more")
        assert len(_draft_frames(client)) == 1
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_partial_text_is_fence_closed_content():
    client = _DraftClient()
    loop = asyncio.get_running_loop()
    sink = ResponseStreamDraft(_fake_adapter(client), "77", loop=loop, min_interval_s=0)
    try:
        sink.on_content("Hello\n```python\nprint(1)")
        assert sink.partial_text == "Hello\n```python\nprint(1)\n```"
        sink.on_content("\nprint(2)\n```")
        assert sink.partial_text == "Hello\n```python\nprint(1)\nprint(2)\n```"
    finally:
        sink.cancel()


@pytest.mark.asyncio
async def test_adapter_routes_stop_press_to_live_sink():
    """A Stop press (UpdateUserTyping + SendMessageStopDraftAction) reaches
    the live sink by random_id; unknown ids and plain typing actions are
    ignored."""
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    client = _DraftClient()
    adapter = TelethonAdapter(PlatformConfig(extra={"streaming_response": True}))
    adapter._client = client
    source = _dm_source()
    sink = adapter.streaming_response_sink(source, asyncio.get_running_loop())
    assert sink is not None
    stopped = []
    sink.on_stop = lambda: stopped.append(True)

    # Plain typing (not a Stop) is ignored.
    await adapter._on_user_typing(SimpleNamespace(
        action=types.SendMessageTypingAction()))
    assert stopped == []

    # A Stop for an unknown random_id is ignored.
    await adapter._on_user_typing(SimpleNamespace(
        action=types.SendMessageStopDraftAction(random_id=sink.random_id + 1)))
    assert stopped == []

    # The Stop for this sink halts it.
    await adapter._on_user_typing(SimpleNamespace(
        action=types.SendMessageStopDraftAction(random_id=sink.random_id)))
    assert stopped == [True]
    assert sink.stop_requested
    assert sink._finished

    # The registry is consumed: a repeat press finds nothing.
    await adapter._on_user_typing(SimpleNamespace(
        action=types.SendMessageStopDraftAction(random_id=sink.random_id)))
    assert stopped == [True]
    sink.cancel()
