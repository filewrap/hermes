"""Interactive Telegram cooldowns must settle without holding the turn in a long retry."""
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent.agent_runtime_helpers import extract_api_error_context
from agent.error_classifier import classify_api_error
from agent.turn_api_error import settle_unrecovered_error
from agent.turn_retry_state import TurnRetryState
from gateway.session_context import clear_session_vars, set_session_vars
from gateway.session_finite import finite_turn_scope

BASE = "https://inference-api.nousresearch.com/v1"
MODEL = "qwen/qwen4-max:free"


class RateLimitError(Exception):
    status_code = 429

    def __init__(self, cooldown):
        self.body = {"error": {"message": "Fair-share rate limit exceeded"}}
        if cooldown is not None:
            self.body["error"]["retry_after"] = cooldown
        super().__init__("HTTP 429: fair-share rate limit exceeded")


def make_agent(**overrides):
    agent = SimpleNamespace(
        platform="telegram", side_agent=False, provider="nous", model=MODEL,
        base_url=BASE, api_key="test-key", log_prefix="", verbose=False, verbose_logging=False,
        _auto_recovery_cycles=5, _current_streamed_assistant_text="", _rate_limit_state=None,
        _has_content_after_think_block=bool, _strip_think_blocks=lambda text: text,
        _has_pending_fallback=lambda: False, _try_activate_fallback=Mock(return_value=False),
        _try_recover_primary_transport=Mock(return_value=False),
        _extract_api_error_context=extract_api_error_context, _summarize_api_error=str, _flush_status_buffer=Mock(), _emit_diagnostic_status=Mock(),
        _persist_session=Mock(), _plines=Mock(), _buffer_status=Mock(), _vprint=Mock(),
        _client_log_context=lambda: "", _buffer_diagnostic_status=Mock(), _emit_diagnostic_wait=Mock(),
    )
    for name, value in overrides.items():
        setattr(agent, name, value)
    return agent


def settle(agent, cooldown):
    error = RateLimitError(cooldown)
    classified = classify_api_error(error, provider=agent.provider, model=agent.model, base_url=agent.base_url)
    return settle_unrecovered_error(
        agent, api_error=error, classified=classified, _retry=TurnRetryState(), status_code=429,
        error_msg=str(error).lower(), is_context_length_error=False, is_rate_limited=True,
        _is_zai_coding_overload=False, _provider=agent.provider, _base=agent.base_url,
        _model=agent.model, messages=[{"role": "user", "content": "hello"}], api_messages=[],
        api_kwargs=None, active_system_prompt="", conversation_history=[], approx_tokens=10,
        retry_count=1, max_retries=3, compression_attempts=0, api_call_count=1,
        error_context=classified.error_context,
    )


@pytest.fixture(autouse=True)
def source_scope():
    tokens = set_session_vars(source="telegram")
    try:
        yield
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize("cooldown", [61, 4553])
def test_public_nous_telegram_long_cooldown_settles_first_attempt_without_wait(cooldown):
    agent = make_agent()
    with patch("agent.turn_api_error.interruptible_backoff_sleep", return_value=None) as sleep, \
            patch("agent.turn_recovery.interruptible_backoff_sleep") as ladder_sleep:
        verdict = settle(agent, cooldown)
    assert verdict.action == "return"
    assert verdict.result["completed"] is False
    assert verdict.result["failed"] is True
    assert verdict.result["failure_reason"] == "rate_limit"
    assert verdict.result["api_calls"] == 1
    assert verdict.result["final_response"]
    assert "usage limit resets in" in verdict.result["final_response"]
    assert verdict.result["failure_resets_at"] == pytest.approx(time.time() + cooldown, abs=2)
    assert "rate-limiting requests right now" in verdict.result["final_response"]
    assert "600" not in verdict.result["final_response"]
    sleep.assert_not_called()
    ladder_sleep.assert_not_called()
    agent._persist_session.assert_called_once()
    assert (agent.model, agent.api_key) == (MODEL, "test-key")


@pytest.mark.parametrize("cooldown", [None, 59, 60])
def test_telegram_short_or_unknown_cooldown_keeps_retry_behavior(cooldown):
    with patch("agent.turn_api_error.interruptible_backoff_sleep", return_value=None) as sleep:
        verdict = settle(make_agent(), cooldown)
    assert verdict.action == "fallthrough"
    sleep.assert_called_once()
    assert verdict.result is None


@pytest.mark.parametrize("overrides", [
    {"platform": "cron"}, {"platform": "subagent"}, {"platform": None},
    {"platform": "cli"}, {"side_agent": True},
])
def test_background_or_child_inheriting_telegram_source_keeps_retry_behavior(overrides):
    with patch("agent.turn_api_error.interruptible_backoff_sleep", return_value=None) as sleep:
        verdict = settle(make_agent(**overrides), 4553)
    assert verdict.action == "fallthrough"
    sleep.assert_called_once()


def test_finite_telegram_turn_keeps_retry_behavior():
    with finite_turn_scope(True), patch("agent.turn_api_error.interruptible_backoff_sleep", return_value=None) as sleep:
        verdict = settle(make_agent(), 4553)
    assert verdict.action == "fallthrough"
    sleep.assert_called_once()


def test_telegram_transport_with_cron_source_keeps_retry_behavior():
    tokens = set_session_vars(source="cron")
    try:
        with patch("agent.turn_api_error.interruptible_backoff_sleep", return_value=None) as sleep:
            verdict = settle(make_agent(), 4553)
        assert verdict.action == "fallthrough"
        sleep.assert_called_once()
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize("display_kind", ["internal_notification", "process_complete", "async_delegation_complete"])
def test_machinery_telegram_turn_keeps_retry_behavior(display_kind):
    """A heartbeat, one-shot wakeup, process-complete or delegation turn keeps its 600 s retry:
    the turn is machinery, not a person waiting. The kind is re-stamped every turn, so the next
    human turn still fails fast."""
    with patch("agent.turn_api_error.interruptible_backoff_sleep", return_value=None) as sleep:
        verdict = settle(make_agent(_turn_display_kind=display_kind), 4553)
    assert verdict.action == "fallthrough"
    sleep.assert_called_once()


def test_wakeup_style_event_carries_machinery_display_kind():
    """The admission half of the exclusion: a one-shot wakeup rides the heartbeat authority lane,
    so its event is internal with a stamped heartbeat session id — and the persisted display kind
    for that event is machinery. Built exactly the way admit_heartbeat builds it."""
    from gateway.platforms.event import MessageEvent
    from gateway.response_filters import display_kind_for_event

    event = MessageEvent(text="wake up", source="telegram", internal=True, message_id='["wakeup","sess","wk_1"]')
    event._heartbeat_session_id = "sess"
    assert display_kind_for_event(event) == "internal_notification"


def test_full_agent_can_start_next_turn_after_cooldown_failure():
    from unittest.mock import MagicMock
    from run_agent import AIAgent
    from gateway.run import _sanitize_gateway_final_response

    with patch("model_tools.get_tool_definitions", return_value=[]), \
            patch("model_tools.check_toolset_requirements", return_value={}), \
            patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(
            provider="nous", api_key="test-key", base_url=BASE, model=MODEL,
            platform="telegram", quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    message = SimpleNamespace(content="second turn works", tool_calls=None, reasoning=None,
                              reasoning_content=None, reasoning_details=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                               model=MODEL, usage=None)
    with patch.object(agent, "_interruptible_api_call", side_effect=[RateLimitError(4553), response]) as request, \
            patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"), \
            patch.object(agent, "_cleanup_task_resources"), \
            patch.object(agent, "_try_recover_primary_transport", return_value=False), \
            patch.object(agent, "_recover_with_credential_pool", return_value=(False, False)), \
            patch.object(agent, "_has_pending_fallback", return_value=False), \
            patch.object(agent, "_try_activate_fallback", return_value=False), \
            patch("agent.turn_api_error.interruptible_backoff_sleep", side_effect=AssertionError("retry sleep reached")), \
            patch("agent.turn_recovery.interruptible_backoff_sleep", side_effect=AssertionError("ladder sleep reached")):
        first = agent.run_conversation("first turn")
        assert first["failed"] is True and first["completed"] is False
        assert first["failure_reason"] == "rate_limit"
        assert request.call_count == 1
        assert _sanitize_gateway_final_response("telegram", first["final_response"])
        assert getattr(agent, "_active_session_turn_lease_holder", None) is None
        second = agent.run_conversation("second turn", conversation_history=first["messages"])
    assert second["completed"] is True
    assert second["final_response"] == "second turn works"
    assert request.call_count == 2
    assert (agent.model, agent.api_key) == (MODEL, "test-key")
