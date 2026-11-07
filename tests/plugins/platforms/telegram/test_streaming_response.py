"""Streaming response drafts (Beta): rich live drafts for Telegram DM turns.

The draft is an ephemeral rich preview (Thinking block while reasoning-only,
Markdown once content flows); the complete final message still goes out once
through the normal delivery path. Groups get normal replies, no edit-loop
anywhere, and every draft failure fails open.
"""
import asyncio
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


def _draft_frames(client):
    """(kind, payload, random_id) per successfully sent draft, in order."""
    frames = []
    for request in client.sent:
        if type(request).__name__ != "SetTypingRequest":
            continue
        action = request.action
        assert request.top_msg_id is None
        if type(action).__name__ == "InputSendMessageRichMessageDraftAction":
            rich = action.rich_message
            if type(rich).__name__ == "InputRichMessage":
                block = rich.blocks[0]
                assert type(block).__name__ == "PageBlockThinking"
                frames.append(("thinking", block.text.text, action.random_id))
            else:
                assert type(rich).__name__ == "InputRichMessageMarkdown"
                frames.append(("rich", rich.markdown, action.random_id))
        else:
            assert type(action).__name__ == "SendMessageTextDraftAction"
            frames.append(("text", action.text.text, action.random_id))
    return frames


def _set_typing_requests(client):
    return [r for r in client.requests if type(r).__name__ == "SetTypingRequest"]


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
        assert frames[0][:2] == ("thinking", "Thinking through it")
        assert frames[1][:2] == ("rich", "Hello")
        assert frames[2][:2] == ("rich", "Hello world")
        # One fixed random_id per generation: the same id animates the draft.
        assert len({frame[2] for frame in frames}) == 1
        assert frames[0][2] != 0
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

        assert [(kind, text) for kind, text, _ in _draft_frames(client)] == [
            ("thinking", "r1"), ("rich", "c1"),
        ]
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
        await async_wait_until(lambda: len(_draft_frames(client)) == 1)
        assert _draft_frames(client)[0][:2] == ("thinking", "r")

        # The held deltas coalesce: the next frame out carries the latest text,
        # whether the gate opens on a new delta or on the pump's flush.
        clock[0] = 102.0
        sink.on_content("c")
        await async_wait_until(lambda: len(_draft_frames(client)) >= 2)
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
        assert _draft_frames(client)[0][:2] == ("thinking", "Thinking through it")

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
