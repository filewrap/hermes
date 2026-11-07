"""GuestBot turns never stream: no draft sink, no typing requests.

The guest draft sink was removed 2026-11-07: guestbot cannot send a typing
indicator (the draft never worked live — 0/17 ``PeerIdInvalidError`` on
``SetTypingRequest``). A guest turn now shows the "Thinking…" status message,
edited in place with the answer, and the final still goes out through the
guest-result path. A legacy ``guest_streaming: true`` in ``config.yaml`` is an
inert orphan: it must not re-attach any sink.

The DM streaming-response sink (``streaming_response_sink_for_turn``) is a
separate path and is untouched; these tests say nothing about it. The
``GatewayStreamConsumer`` is likewise untouched: on Telegram it can only use
the send/edit path (the adapter reports no draft or native streaming), so it
never emits a typing/draft action for any turn, guest or not.
"""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig, StreamingConfig
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class _DraftClient:
    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace()


def _typing_requests(client):
    return [r for r in client.requests if type(r).__name__ == "SetTypingRequest"]


def _guest_turn(*, streaming, thread_id="guest-77"):
    loop = asyncio.get_running_loop()
    client = _DraftClient()
    adapter = TelethonAdapter(PlatformConfig(
        extra={"guest_streaming": streaming} if streaming else {}))
    adapter._client = client
    ctx = TurnContext(
        streaming_tts_consumer_holder=[None], user_config={},
        resolve_display_setting=lambda *args: None,
        interim_assistant_messages_enabled=False,
        source=SimpleNamespace(platform=Platform.TELEGRAM, chat_id="77", thread_id=thread_id),
        _run_still_current=lambda: True,
    )
    ctx._loop_for_step = loop
    runner = SimpleNamespace(
        config=SimpleNamespace(streaming=StreamingConfig()),
        _delivery_adapter_for=lambda source: adapter,
        _build_stream_consumer_config=lambda *args, **kwargs: (None, None),
    )
    turn = TurnRunner(runner, ctx)
    return turn, adapter, client


@pytest.mark.asyncio
async def test_setup_stream_consumer_attaches_no_guest_sink():
    """Even with the legacy ``guest_streaming: true`` flag, a guest turn gets
    no draft sink: no consumer, no delta callback, no reasoning callback."""
    turn, _adapter, _client = _guest_turn(streaming=True)

    consumer, delta_cb, _interim, want_interim, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert consumer is None
    assert delta_cb is None
    assert reasoning_cb is None
    assert want_interim is False


@pytest.mark.asyncio
async def test_setup_stream_consumer_attaches_no_guest_sink_when_flag_off():
    turn, _adapter, _client = _guest_turn(streaming=False)

    consumer, delta_cb, _interim, want_interim, reasoning_cb = turn._setup_stream_consumer("telegram")

    assert consumer is None
    assert delta_cb is None
    assert reasoning_cb is None
    assert want_interim is False


@pytest.mark.asyncio
async def test_guest_turn_deltas_emit_no_typing_requests():
    """Whatever callbacks a guest turn wires, driving them must never produce
    a ``SetTypingRequest``: there is no guest draft sink left to feed."""
    turn, _adapter, client = _guest_turn(streaming=True)

    _consumer, delta_cb, _interim, _want, reasoning_cb = turn._setup_stream_consumer("telegram")
    content = "Hello guest! " + "0123456789" * 25
    if reasoning_cb is not None:
        reasoning_cb("Thinking through it")
    if delta_cb is not None:
        delta_cb(content)
    # Draft sends are scheduled onto the loop; yield so any sink that exists fires.
    for _ in range(10):
        await asyncio.sleep(0)

    assert _typing_requests(client) == []


def _guest_update(query_id, message_id):
    from telethon import types

    message = SimpleNamespace(
        peer_id=types.PeerUser(user_id=77), from_id=types.PeerUser(user_id=77),
        id=message_id, message="Can you help?", reply_to=None, reply_to_msg_id=None,
    )
    return SimpleNamespace(query_id=query_id, message=message)


@pytest.mark.asyncio
async def test_guest_query_sends_one_final_and_no_typing_requests():
    """End to end through the adapter's guest-query path with a turn that
    wires its callbacks the way the real runner does: exactly one guest
    result, zero ``SetTypingRequest`` calls, even with the legacy flag on."""
    turn, adapter, client = _guest_turn(streaming=True)
    adapter._authorized = lambda _source: True
    content = "Hello guest! " + "0123456789" * 25

    async def handle(event):
        _consumer, delta_cb, _interim, _want, reasoning_cb = turn._setup_stream_consumer("telegram")
        if reasoning_cb is not None:
            reasoning_cb("Thinking through it")
        await asyncio.sleep(0)
        if delta_cb is not None:
            delta_cb(content)
        await asyncio.sleep(0)
        return content

    adapter._message_handler = handle
    await adapter._on_guest_chat_query(_guest_update(1234, 500))
    for _ in range(10):
        await asyncio.sleep(0)

    assert _typing_requests(client) == []
    results = [r for r in client.requests if type(r).__name__ == "SetBotGuestChatResultRequest"]
    assert len(results) == 1
    assert results[0].query_id == 1234
    assert results[0].result.send_message.rich_message.markdown == content


def _wire(reasoning_cb=None):
    """Run `_wire_turn_agent_callbacks` over minimal fakes; return the agent."""
    agent = SimpleNamespace()
    ctx = SimpleNamespace(
        progress_callback=None,
        native_tool_start_callback=None,
        voice_ack_callback=None,
        _voice_ack_guild=[None],
        voice_turn=False,
        _native_slack_task_cards=False,
        native_tool_complete_callback=None,
        _step_callback_sync=None,
        _hooks_ref=SimpleNamespace(loaded_hooks=[]),
        _status_callback_sync=None,
        _event_callback_sync=None,
        _status_adapter=None,
        session_key="",
        user_config={},
        source=SimpleNamespace(platform="telegram"),
        mute_notification_reply=False,
        _thinking_enabled=False,
        agent_holder=[None],
        tools_holder=[None],
        process_task_id=None,
        process_baseline=None,
        run_generation=0,
    )
    holder = SimpleNamespace(
        _ctx=ctx,
        _approval_owner=None,
        _runner=SimpleNamespace(
            _service_tier=None,
            _consume_pending_turn_sidecar_notes=lambda key: [],
        ),
        _make_bg_review_callbacks=lambda: (lambda message: None, lambda: None),
        _clarify_callback_sync=lambda *a, **k: None,
        _notice_callback_sync=lambda *a, **k: None,
        _attach_session_title_callback=lambda agent, ctx: None,
    )
    delta = lambda text: None
    TurnRunner._wire_turn_agent_callbacks(holder, agent, {}, None, delta, None, False, reasoning_cb)
    return agent, delta


def test_wire_turn_agent_callbacks_installs_reasoning_callback():
    reasoning_cb = lambda text: None
    agent, delta = _wire(reasoning_cb)
    assert agent.reasoning_callback is reasoning_cb
    assert agent.stream_delta_callback is delta


def test_wire_turn_agent_callbacks_defaults_reasoning_callback_to_none():
    agent, _delta = _wire()
    assert agent.reasoning_callback is None
