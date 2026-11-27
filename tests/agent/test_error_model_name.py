"""Failed turns name the model they used (short form, vendor prefix dropped).

A turn that fails on a model must say so in the user-facing error text: rate
limits, timeouts, server errors and other provider errors previously named only
the provider, so a failure on a retired or wrong model read exactly like a
failure on the right one. model_not_found already names the model in its body
and is left exactly as it is.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.conversation_loop import _billing_failure_result
from agent.error_classifier import ClassifiedError, FailoverReason, classify_api_error
from agent.turn_failure_copy import content_policy_copy, model_name_line
from agent.turn_recovery import max_retries_exhausted_result, nonretryable_client_error_result
from agent.turn_response_check import retry_invalid_response
from agent.turn_retry_state import TurnRetryState

_MODEL = "nous/crouton-alpha"


def test_model_name_line_drops_vendor_prefix():
    assert model_name_line("nous/crouton-alpha") == "\n\nModel: crouton-alpha"
    assert model_name_line("crouton-alpha") == "\n\nModel: crouton-alpha"


def test_model_name_line_empty_when_unknown():
    assert model_name_line("") == ""
    assert model_name_line(None) == ""
    assert model_name_line("unknown") == ""


def test_content_policy_copy_names_model():
    text = content_policy_copy(label="Nous Portal", summary="blocked", model=_MODEL)
    assert "safety filter refused" in text
    assert text.endswith("\n\nModel: crouton-alpha")


def test_content_policy_copy_without_model_has_no_model_line():
    text = content_policy_copy(label="Nous Portal", summary="blocked")
    assert "Model:" not in text


class _Failed(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class _Agent:
    log_prefix = ""
    verbose = False

    def _summarize_api_error(self, error):
        return str(error)

    def __getattr__(self, name):  # status/persist/print helpers the terminal path calls
        return lambda *args, **kwargs: None


def _max_retries_result(status_code: int, message: str, *, is_rate_limited: bool, attempts: int = 1):
    error = _Failed(status_code, message)
    classified = classify_api_error(error, provider="nous", model=_MODEL)
    return max_retries_exhausted_result(
        _Agent(), error, classified, attempts=attempts, is_rate_limited=is_rate_limited,
        error_msg=str(error), api_kwargs=None, api_messages=[], messages=[],
        conversation_history=None, api_call_count=1, approx_tokens=10, provider="nous",
        base_url="https://example.com/v1", model=_MODEL,
    )


def test_max_retries_rate_limit_names_model():
    result = _max_retries_result(429, "HTTP 429: rate limited", is_rate_limited=True)
    assert "rate-limit" in result["final_response"].lower()
    assert result["final_response"].endswith("\n\nModel: crouton-alpha")


def test_max_retries_server_error_names_model():
    result = _max_retries_result(500, "HTTP 500: internal error", is_rate_limited=False, attempts=3)
    assert "server error" in result["final_response"].lower()
    assert result["final_response"].endswith("\n\nModel: crouton-alpha")


def test_nonretryable_auth_names_model():
    error = _Failed(401, "HTTP 401: User not found.")
    classified = classify_api_error(error, provider="nous", model=_MODEL)
    result = nonretryable_client_error_result(
        _Agent(), error, classified, status_code=401, api_kwargs=None, api_messages=[], messages=[],
        conversation_history=None, api_call_count=1, approx_tokens=10, provider="nous",
        base_url="https://example.com/v1", model=_MODEL,
    )
    assert result["final_response"].endswith("\n\nModel: crouton-alpha")


def test_nonretryable_model_not_found_left_exactly_as_it_is():
    error = _Failed(404, "HTTP 404: model not found")
    classified = classify_api_error(error, provider="nous", model=_MODEL)
    assert classified.reason.value == "model_not_found"
    result = nonretryable_client_error_result(
        _Agent(), error, classified, status_code=404, api_kwargs=None, api_messages=[], messages=[],
        conversation_history=None, api_call_count=1, approx_tokens=10, provider="nous",
        base_url="https://example.com/v1", model=_MODEL,
    )
    # The body already names the model; no trailing Model: line is added.
    assert "Model 'nous/crouton-alpha' isn't available" in result["final_response"]
    assert "\n\nModel:" not in result["final_response"]


def test_billing_failure_names_model():
    error = _Failed(402, "HTTP 402: out of credits")
    classified = classify_api_error(error, provider="nous", model=_MODEL)
    result = _billing_failure_result(
        classified=classified, summary="HTTP 402: out of credits", messages=[], api_call_count=1,
        provider="nous", base_url="https://example.com/v1", model=_MODEL,
        guidance="Add credits or switch models.",
    )
    assert result["final_response"].endswith("\n\nModel: crouton-alpha")


def test_welcome_refusal_keeps_sign_in_door_last_and_no_model_line():
    # A welcome refusal is a sign-in door, not a provider error about a model:
    # its last line is the call to action, so the model line stays off it.
    error = _Failed(429, "HTTP 429: rate limited")
    classified = ClassifiedError(
        reason=FailoverReason.rate_limit, status_code=429,
        error_context={"welcome_refusal": {"reason": "at_capacity", "retry_after": 0}},
        retryable=True,
    )
    result = max_retries_exhausted_result(
        _Agent(), error, classified, attempts=3, is_rate_limited=True,
        error_msg=str(error), api_kwargs=None, api_messages=[], messages=[],
        conversation_history=None, api_call_count=1, approx_tokens=10, provider="nous",
        base_url="https://example.com/v1", model=_MODEL,
    )
    assert result["final_response"].endswith("To sign in: /login.")
    assert "\n\nModel:" not in result["final_response"]


def test_invalid_response_names_model():
    agent = MagicMock()
    agent.log_prefix, agent.quiet_mode, agent.api_mode = "", True, "chat_completions"
    agent.provider, agent.model = "nous", _MODEL
    agent._fallback_chain, agent._fallback_index = [], 0
    agent._try_activate_fallback.return_value = False
    verdict = retry_invalid_response(
        agent, response=SimpleNamespace(choices=[], error=None),
        error_details=["response.choices is empty"],
        _retry=TurnRetryState(), thinking_spinner=None, messages=[], api_messages=[], api_kwargs=None,
        active_system_prompt=None, conversation_history=None, retry_count=3, max_retries=3,
        compression_attempts=0, api_call_count=1, api_request_id="r", api_start_time=0.0,
        api_duration=0.4, effective_task_id="t", turn_id="turn",
    )
    assert verdict.action == "return"
    assert verdict.result["final_response"].endswith("\n\nModel: crouton-alpha")
