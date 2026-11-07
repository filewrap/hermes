"""Reasoning callback fallback for streaming turns.

A turn with ``stream_delta_callback`` wired used to suppress the fallback in
``_assistant_reasoning_text`` — "fire the accumulated reasoning only when no
stream callback is wired" — and a guest turn always had one (its draft sink
joined the turn's delta sinks), so a provider that returns reasoning only in
the final response (no reasoning deltas during the stream) lost the reasoning
entirely: the guest draft never showed it. The guest draft sink was removed
2026-11-07, but the fallback logic stays: it fires unless the reasoning
already reached the reasoning callback as deltas during this response
(``_reasoning_delta_fired``, set by ``_fire_reasoning_delta`` for native and
inline <think> deltas alike, reset per response in
``_reset_stream_delivery_tracking``). These tests assert both sides: the
reasoning IS delivered when nothing was streamed, and it is NOT repeated
when it was.
"""
from types import SimpleNamespace

from agent.agent_runtime_helpers import extract_reasoning
from agent.chat_completion_helpers import _assistant_reasoning_text
from agent.stream_delivery import StreamDeliveryMixin

_REASONING = "Thinking through it"


class _GuestAgent(StreamDeliveryMixin):
    """A streamed-turn agent: stream callbacks wired, reasoning not yet streamed."""


def _guest_agent():
    agent = _GuestAgent()
    agent.verbose_logging = False
    # A stream delta callback wired alone must not suppress the reasoning
    # fallback (a guest turn used to always have one, via its draft sink).
    agent.stream_delta_callback = lambda text: None
    agent._stream_callback = None
    agent._reasoning_delta_fired = False
    agent._extract_reasoning = lambda msg: extract_reasoning(agent, msg)
    return agent


def _assistant_message():
    return SimpleNamespace(reasoning_content=_REASONING, content="Hello guest!")


def test_guest_turn_reasoning_delivered_when_not_streamed():
    """The fix: reasoning returned only in the final response still reaches the
    reasoning callback even with a stream callback wired."""
    agent = _guest_agent()
    delivered = []
    agent.reasoning_callback = delivered.append

    result = _assistant_reasoning_text(agent, _assistant_message())

    assert delivered == [_REASONING]
    assert result == _REASONING


def test_guest_turn_reasoning_not_repeated_when_streamed():
    """The guard: reasoning already delivered as a delta during the stream is not
    fired again by the post-response fallback."""
    agent = _guest_agent()
    delivered = []
    agent.reasoning_callback = delivered.append
    agent._fire_reasoning_delta(_REASONING)

    assert agent._reasoning_delta_fired is True
    _assistant_reasoning_text(agent, _assistant_message())

    assert delivered == [_REASONING]


def test_fire_reasoning_delta_marks_fired_flag():
    """A reasoning delta that reaches the callback marks the response as streamed."""
    agent = _GuestAgent()
    delivered = []
    agent.reasoning_callback = delivered.append
    agent._reasoning_delta_fired = False

    agent._fire_reasoning_delta(_REASONING)

    assert delivered == [_REASONING]
    assert agent._reasoning_delta_fired is True


def test_fire_reasoning_delta_without_callback_leaves_flag_clear():
    """No callback, nothing delivered: the fallback must still fire later."""
    agent = _GuestAgent()
    agent.reasoning_callback = None
    agent._reasoning_delta_fired = False

    agent._fire_reasoning_delta(_REASONING)

    assert agent._reasoning_delta_fired is False


def test_reset_stream_delivery_tracking_clears_reasoning_flag():
    """The flag is per response: a new API call starts with it clear."""
    agent = _GuestAgent()
    agent._reasoning_delta_fired = True

    agent._reset_stream_delivery_tracking()

    assert agent._reasoning_delta_fired is False
