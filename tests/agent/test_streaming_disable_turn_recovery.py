"""A streaming failure must not latch the session off streaming forever.

One bad frame (empty SSE keepalive, stream rejected, Anthropic events out of
order, Bedrock IAM denial, an adapter returning a completed response) set the
sticky per-agent ``_disable_streaming`` flag, and the cached agent kept it for
the rest of the session. Every later turn then ran non-streaming, so the
reasoning only existed in the final response and ``_assistant_reasoning_text``
dumped it whole — the live symptom: ~13s of nothing, then the full reasoning
at once, cut off mid-word as content took over.

Contract:
- each failure records a reason; the next turn re-probes streaming
  (``_should_stream`` True again after a turn boundary);
- the replay path delivers reasoning as multiple ``_fire_reasoning_delta``
  calls whose concatenation is identical to the original text;
- a config-seeded ``model.streaming: false`` (no reason recorded) stays off;
- each disable notice fires at most once per reason per session.
"""
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as h
from agent.conversation_loop import run_conversation
from agent.error_classifier import PROVIDER_STREAM_EMPTY_FRAME_ERROR_CODE
from agent.turn_api_call import _should_stream


def _agent(**overrides):
    """Fake session agent: stream consumers present, streaming on unless told otherwise."""
    sinks = {"warnings": [], "prints": [], "reasoning": [], "content": []}
    agent = SimpleNamespace(
        provider="custom", model="m", base_url="http://127.0.0.1:8080/v1",
        api_mode="chat_completions", log_prefix="",
        _disable_streaming=False, _streaming_disable_reason=None,
        _streaming_disable_warned_reasons=set(),
        _emit_warning=lambda msg: sinks["warnings"].append(msg),
        _safe_print=lambda *a, **k: sinks["prints"].append("".join(str(x) for x in a)),
        _fire_reasoning_delta=lambda text: sinks["reasoning"].append(text),
        _fire_stream_delta=lambda text: sinks["content"].append(text),
        _has_stream_consumers=lambda: True,
        client=None,
    )
    agent.__dict__.update(overrides)
    return agent, sinks


def _make_call(agent):
    call = h._StreamingCall.__new__(h._StreamingCall)
    call.agent = agent
    call.first_delta_fired = {"done": True}
    call.on_first_delta = None
    return call


def _make_bedrock_stream(agent):
    stream = h._BedrockStream.__new__(h._BedrockStream)
    stream.agent = agent
    stream.api_kwargs = {"model": "m"}
    return stream


class _EmptyFrameError(Exception):
    """Stands in for the translated contentless-SSE-frame error."""

    def __init__(self):
        super().__init__("empty SSE frame")
        self.body = {"error": {"code": PROVIDER_STREAM_EMPTY_FRAME_ERROR_CODE}}


def _run_turn(monkeypatch, agent):
    """Drive ``run_conversation`` with the turn body stubbed; record whether
    streaming was re-enabled by the time the turn body ran."""
    seen = {}

    def _fake_turn(agent_arg, user_message, **kwargs):
        seen["should_stream_at_turn_start"] = _should_stream(agent_arg)
        return {"final_response": "ok", "messages": [], "completed": True}

    monkeypatch.setattr("agent.conversation_loop._run_conversation_turn", _fake_turn)
    run_conversation(agent, "hi")
    return seen


def test_empty_sse_frame_disables_with_reason_and_next_turn_reprobes(monkeypatch):
    agent, sinks = _agent()
    _make_call(agent)._maybe_disable_streaming(_EmptyFrameError())

    assert agent._disable_streaming is True
    assert agent._streaming_disable_reason == "empty_sse_frame"
    assert _should_stream(agent) is False
    assert len(sinks["warnings"]) == 1
    assert "stays off for this session" not in sinks["warnings"][0]

    seen = _run_turn(monkeypatch, agent)

    assert seen["should_stream_at_turn_start"] is True
    assert agent._disable_streaming is False
    assert agent._streaming_disable_reason is None


@pytest.mark.parametrize("error,api_mode,reason", [
    (RuntimeError("streaming is not supported for this model"), "chat_completions", "stream_unsupported"),
    (RuntimeError("unexpected event order in Anthropic stream"), "anthropic_messages", "anthropic_out_of_order"),
    (RuntimeError("User is not authorized to perform bedrock:InvokeModelWithResponseStream "
                  "(AccessDeniedException)"), "chat_completions", "bedrock_iam_denied"),
])
def test_stream_rejection_records_reason_and_next_turn_reprobes(monkeypatch, error, api_mode, reason):
    agent, sinks = _agent(api_mode=api_mode)
    _make_call(agent)._maybe_disable_streaming(error)

    assert agent._disable_streaming is True
    assert agent._streaming_disable_reason == reason
    assert _should_stream(agent) is False
    assert len(sinks["prints"]) == 1

    seen = _run_turn(monkeypatch, agent)

    assert seen["should_stream_at_turn_start"] is True
    assert agent._disable_streaming is False
    assert agent._streaming_disable_reason is None


def test_bedrock_converse_fallback_records_iam_reason():
    agent, sinks = _agent(provider="bedrock")
    stream = _make_bedrock_stream(agent)

    class _Client:
        def converse(self, **kwargs):
            return {"output": {"message": {"content": [{"text": "ok"}]}},
                    "usage": {}, "stopReason": "end_turn", "modelId": "m"}

    result = stream._fall_back_to_converse(_Client(), {"modelId": "m"}, RuntimeError("denied"))

    assert result.choices[0].message.content == "ok"
    assert agent._disable_streaming is True
    assert agent._streaming_disable_reason == "bedrock_iam_denied"
    assert len(sinks["prints"]) == 1


def test_final_response_adoption_records_reason():
    agent, sinks = _agent()
    response = SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content="hi", reasoning_content=None))])

    assert _make_call(agent)._adopt_final_response(response) is response
    assert agent._disable_streaming is True
    assert agent._streaming_disable_reason == "final_response_instead_of_iterator"
    assert sinks["content"] == ["hi"]


def test_config_seeded_streaming_false_stays_off_across_turns(monkeypatch):
    # No reason recorded: the per-turn reset must never clear a config seed.
    agent, _sinks = _agent(_disable_streaming=True, _streaming_disable_reason=None)

    seen = _run_turn(monkeypatch, agent)

    assert seen["should_stream_at_turn_start"] is False
    assert agent._disable_streaming is True
    assert agent._streaming_disable_reason is None


def test_disable_notice_fires_once_per_reason(monkeypatch):
    agent, sinks = _agent()
    call = _make_call(agent)

    call._maybe_disable_streaming(_EmptyFrameError())
    assert len(sinks["warnings"]) == 1
    _run_turn(monkeypatch, agent)  # turn boundary: streaming re-probed, notice must not repeat
    call._maybe_disable_streaming(_EmptyFrameError())
    assert len(sinks["warnings"]) == 1, "same reason must not warn twice in a session"

    # A different reason still warns once, and does not re-arm the first reason.
    call._maybe_disable_streaming(RuntimeError("streaming is not supported for this model"))
    assert len(sinks["prints"]) == 1
    _run_turn(monkeypatch, agent)
    call._maybe_disable_streaming(_EmptyFrameError())
    assert len(sinks["warnings"]) == 1


_LONG_REASONING = (
    "First paragraph, sentence one. Sentence two follows here, with a bit more detail.\n\n"
    "Second paragraph carries the next thought. It also has two sentences, like the first.\n\n"
    "Third paragraph is one very long sentence with no place to break it " + "and it keeps going " * 40 + "until the end."
)


def test_replay_splits_reasoning_into_incremental_deltas():
    agent, sinks = _agent()
    response = SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content="final answer", reasoning_content=_LONG_REASONING))])

    _make_call(agent)._replay_final_response(response)

    assert len(sinks["reasoning"]) > 1, "replay must deliver reasoning as several deltas, not one blob"
    assert "".join(sinks["reasoning"]) == _LONG_REASONING
    assert sinks["content"] == ["final answer"]


@pytest.mark.parametrize("text", [
    "",
    "short",
    "x" * 2000,  # no boundaries at all: fixed-size fallback must stay lossless
    "word " * 500,  # spaces but no sentence boundaries
    "Trailing whitespace stays.  \n\n\n",
    "Ünïcödé reasoning — with em dashes… and “quotes”. " * 30,
    "Ends mid-sentence without punctuation " * 30,
])
def test_split_reasoning_replay_is_lossless(text):
    pieces = h._split_reasoning_replay(text)

    assert "".join(pieces) == text
    assert all(piece for piece in pieces)
    if len(text) > 400:
        assert len(pieces) > 1
        assert all(len(piece) <= 400 for piece in pieces)
    else:
        assert pieces == ([text] if text else [])
