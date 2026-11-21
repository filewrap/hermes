"""Regression for #115702: a paid Nous model behind an empty credit balance answers HTTP 404
``insufficient_credits_for_paid_model``. The code must classify as billing (fallback chain armed,
no retry burn) and the resulting switch must be a WARNING naming the failing profile and remedy.
"""

import logging

from agent.chat_completion_helpers import _log_fallback_activated
from agent.error_classifier import FailoverReason, classify_api_error
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


class _StatusError(Exception):
    def __init__(self, message, status_code, body):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def test_404_insufficient_credits_code_is_billing_with_fallback():
    # Structured code only, message carries no billing wording (message-pattern rules cannot save it).
    err = _StatusError(
        "Not Found", 404, {"error": {"code": "insufficient_credits_for_paid_model", "message": "Not Found"}},
    )
    verdict = classify_api_error(err, provider="nous", model="z-ai/glm-5.2")
    assert verdict.reason == FailoverReason.billing
    assert verdict.should_fallback and not verdict.retryable
    # Control: an unrelated 404 body keeps its generic verdict — nothing to fall back for.
    other = classify_api_error(
        _StatusError("Not Found", 404, {"error": {"code": "route_not_found", "message": "Not Found"}}),
        provider="nous", model="z-ai/glm-5.2",
    )
    assert other.reason == FailoverReason.unknown


def test_billing_fallback_warning_names_failing_profile_and_remedy(tmp_path, caplog):
    """Under multiplex the log runs in the failing profile's home scope: A then B name themselves,
    never the launch profile; a non-billing switch stays INFO."""
    seen = {}
    for name in ("alpha", "beta"):
        token = set_hermes_home_override(tmp_path / ".hermes" / "profiles" / name)
        try:
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="agent.chat_completion_helpers"):
                _log_fallback_activated(None, FailoverReason.billing, "z-ai/glm-5.2", "nous", "free/model", "nous")
        finally:
            reset_hermes_home_override(token)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        seen[name] = warnings[0].getMessage()
    assert "Profile alpha:" in seen["alpha"] and "hermes -p alpha model" in seen["alpha"]
    assert "Profile beta:" in seen["beta"] and "alpha" not in seen["beta"]
    for text in seen.values():
        assert "z-ai/glm-5.2" in text and "free/model" in text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agent.chat_completion_helpers"):
        _log_fallback_activated(None, FailoverReason.server_error, "a", "p", "b", "q")
    assert [r.levelno for r in caplog.records] == [logging.INFO]

def test_openrouter_retired_model_404_is_model_not_found_not_retried():
    """The host log's 404: OpenRouter retired meta-llama/llama-3.3-8b-instruct:free
    ("no provider serves this model anymore"). Retrying a retired model is
    deterministic waste — it must classify model_not_found (fail fast, fall back),
    never ``unknown`` (retryable, no fallback)."""
    err = _StatusError(
        "meta-llama/llama-3.3-8b-instruct:free is no longer available on OpenRouter: "
        "no provider serves this model anymore. See available models at https://openrouter.ai/models",
        404,
        {"error": {"code": 404, "message": "meta-llama/llama-3.3-8b-instruct:free is no longer "
         "available on OpenRouter: no provider serves this model anymore"}},
    )
    verdict = classify_api_error(
        err, provider="openrouter", model="meta-llama/llama-3.3-8b-instruct:free",
    )
    assert verdict.reason == FailoverReason.model_not_found
    assert verdict.status_code == 404
    assert verdict.should_fallback and not verdict.retryable

def _retired_model_error():
    return _StatusError(
        "meta-llama/llama-3.3-8b-instruct:free is no longer available on OpenRouter: "
        "no provider serves this model anymore. See available models at https://openrouter.ai/models",
        404,
        {"error": {"code": 404, "message": "meta-llama/llama-3.3-8b-instruct:free is no longer "
         "available on OpenRouter: no provider serves this model anymore"}},
    )


def _loop_agent():
    from unittest.mock import MagicMock, patch

    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=None,
        )
        agent.client = MagicMock()
        agent.provider = "openrouter"
        agent.model = "meta-llama/llama-3.3-8b-instruct:free"
        return agent


def _settle(agent, err, classified, *, retry_count=0):
    from unittest.mock import patch

    from agent.turn_api_error import settle_unrecovered_error
    from agent.turn_retry_state import TurnRetryState

    with (
        patch("agent.conversation_loop._arm_fallback_restart", return_value="sys"),
        patch("agent.conversation_loop._credential_refresh_profile", return_value=None),
        patch("agent.turn_api_error.compute_error_backoff", return_value=0),
        patch("agent.turn_api_error.interruptible_backoff_sleep", return_value=None),
    ):
        return settle_unrecovered_error(
            agent, api_error=err, classified=classified, _retry=TurnRetryState(),
            status_code=getattr(err, "status_code", None), error_msg=str(err),
            is_context_length_error=False, is_rate_limited=False,
            _is_zai_coding_overload=False, _provider="openrouter",
            _base="https://openrouter.ai/api/v1",
            _model="meta-llama/llama-3.3-8b-instruct:free",
            messages=[], api_messages=[{"role": "user", "content": "hi"}],
            api_kwargs={}, active_system_prompt="sys", conversation_history=[],
            approx_tokens=100, retry_count=retry_count, max_retries=3,
            compression_attempts=0, api_call_count=1,
        )


def test_retired_model_404_goes_to_fallback_without_burning_retries():
    """The loop fate of the retired-model 404: the unrecovered-error handler must
    arm the fallback chain on the FIRST attempt (``"break"``) — not burn the
    retry budget and only fall back at exhaustion, as the host log showed."""
    err = _retired_model_error()
    classified = classify_api_error(
        err, provider="openrouter", model="meta-llama/llama-3.3-8b-instruct:free",
    )
    agent = _loop_agent()
    calls = []
    agent._try_activate_fallback = lambda reason=None, reset_at=None, **kwargs: calls.append(reason) or True
    verdict = _settle(agent, err, classified, retry_count=0)
    assert verdict.action == "break"
    assert calls == [FailoverReason.model_not_found]


def test_rate_limit_still_retries_instead_of_falling_back():
    """Control: a retryable rate limit keeps the retry path (``"fallthrough"``)
    — the fail-fast change is for deterministic 404/402 only."""
    err = _StatusError("Rate limit exceeded", 429, {"error": {"code": 429, "message": "Rate limit exceeded"}})
    classified = classify_api_error(err, provider="openrouter", model="meta-llama/llama-3.3-8b-instruct:free")
    assert classified.reason == FailoverReason.rate_limit and classified.retryable
    agent = _loop_agent()
    calls = []
    agent._try_activate_fallback = lambda reason=None, reset_at=None, **kwargs: calls.append(reason) or True
    verdict = _settle(agent, err, classified, retry_count=0)
    assert verdict.action == "fallthrough"
    assert calls == []
