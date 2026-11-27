"""Streaming draft in forum topics (experiment): the "(Thinking Ns)" typing
draft must show in the topic where the turn happens, not in General.

The draft is a Telegram typing draft (messages.SetTypingRequest), which
takes an optional top_msg_id. The sink is created only for DM turns today,
so in a forum topic there is no sink at all and no draft anywhere. This
test pins the experiment: a forum-topic turn gets a sink carrying the topic
id, and its SetTypingRequest carries top_msg_id; a plain group still gets
no sink. The user live-tests whether the draft actually shows in the
topic; this file only pins the wire behavior.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telethon import functions, types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.streaming_response import ResponseStreamDraft
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _RecordingClient:
    """Fake Telethon client: records every request, never raises."""

    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace()


def _typing_requests(client):
    return [r for r in client.requests if type(r).__name__ == "SetTypingRequest"]


def _sink_for(source, extra):
    adapter = TelethonAdapter(PlatformConfig(extra=extra))
    adapter._client = _RecordingClient()
    return adapter.streaming_response_sink(source, None)


def _forum_source(thread_id):
    return SimpleNamespace(chat_id="-100123", thread_id=thread_id, chat_type="forum")


def _sink_with_peer(top_msg_id):
    client = _RecordingClient()
    adapter = SimpleNamespace(_client=client)
    sink = ResponseStreamDraft(adapter, "-100123", top_msg_id=top_msg_id)
    sink._peer = types.InputPeerChannel(123, 456)
    return sink, client


def test_forum_topic_turn_gets_sink_carrying_topic_id():
    sink = _sink_for(_forum_source("12345"), {"streaming_response": True})
    assert sink is not None
    assert sink._top_msg_id == 12345


def test_forum_general_turn_gets_sink_with_top_msg_id_1():
    sink = _sink_for(_forum_source("1"), {"streaming_response": True})
    assert sink is not None
    assert sink._top_msg_id == 1


def test_plain_group_still_gets_no_sink():
    source = SimpleNamespace(chat_id="-100123", thread_id=None, chat_type="group")
    assert _sink_for(source, {"streaming_response": True}) is None


def test_forum_guest_thread_gets_no_sink():
    source = SimpleNamespace(chat_id="-100123", thread_id="guest-77", chat_type="forum")
    assert _sink_for(source, {"streaming_response": True}) is None


def test_forum_topic_with_streaming_off_gets_no_sink():
    assert _sink_for(_forum_source("12345"), {}) is None


def test_dm_sink_carries_no_top_msg_id():
    source = SimpleNamespace(chat_id="77", thread_id=None, chat_type="dm")
    sink = _sink_for(source, {"streaming_response": True})
    assert sink is not None
    assert sink._top_msg_id is None


@pytest.mark.asyncio
async def test_topic_sink_sends_top_msg_id_on_frame():
    sink, client = _sink_with_peer(12345)
    assert await sink._send_frame("text", "hello")
    requests = _typing_requests(client)
    assert len(requests) == 1
    assert requests[0].top_msg_id == 12345


@pytest.mark.asyncio
async def test_dm_sink_sends_no_top_msg_id_on_frame():
    sink, client = _sink_with_peer(None)
    assert await sink._send_frame("text", "hello")
    requests = _typing_requests(client)
    assert len(requests) == 1
    assert requests[0].top_msg_id is None


@pytest.mark.asyncio
async def test_clear_draft_carries_top_msg_id():
    sink, client = _sink_with_peer(12345)
    sink._last_sent_key = ("text", "hello")
    assert await sink.clear_draft()
    requests = _typing_requests(client)
    assert len(requests) == 1
    assert requests[0].top_msg_id == 12345
    assert type(requests[0].action).__name__ == "SendMessageCancelAction"
