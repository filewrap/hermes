from types import SimpleNamespace

from agent.error_classifier import FailoverReason, classify_api_error
from agent.error_surface import LAYER_GATEWAY, build_error_surface_from_result
from agent.turn_api_error import settle_unrecovered_error
from agent.turn_failure_copy import nonretryable_copy
from agent.strict_free_policy import StrictFreeOnlyError
from gateway.run_turn_runner import TurnRunner


def test_strict_free_policy_error_classifies_as_local_and_nonretryable():
    error = StrictFreeOnlyError("Request model override rejected by strict free-only mode")

    classified = classify_api_error(error, provider="openrouter", model="openrouter/free")

    assert classified.reason is FailoverReason.strict_free_policy
    assert classified.retryable is False
    assert classified.should_fallback is False
    assert classified.should_rotate_credential is False
    assert classified.message == str(error)


def test_strict_free_policy_error_returns_without_retry_or_fallback(monkeypatch):
    from agent import fallback_cooldown
    from agent import turn_api_error

    class Agent:
        provider = "openrouter"
        model = "openrouter/free"
        fallback_checks = 0
        fallback_attempts = 0

        def _has_pending_fallback(self):
            self.fallback_checks += 1
            return True

        def _try_activate_fallback(self):
            self.fallback_attempts += 1
            return True

    agent = Agent()
    error = StrictFreeOnlyError("Request field 'models' is forbidden in strict free-only mode")
    classified = classify_api_error(error, provider=agent.provider, model=agent.model)
    monkeypatch.setattr(fallback_cooldown, "_mark_entitlement_rejected_model", lambda *_: None)
    monkeypatch.setattr(
        turn_api_error,
        "nonretryable_client_error_result",
        lambda *_args, **_kwargs: {"failure_reason": "strict_free_policy", "failed": True},
    )
    retry = SimpleNamespace(image_shrink_retry_attempted=False, reasoning_mandatory_retry_attempted=False,
                            provider_stale_refresh_attempted=False)

    verdict = settle_unrecovered_error(
        agent,
        api_error=error,
        classified=classified,
        _retry=retry,
        status_code=None,
        error_msg=str(error),
        is_context_length_error=False,
        is_rate_limited=False,
        _is_zai_coding_overload=False,
        _provider=agent.provider,
        _base="https://openrouter.ai/api/v1",
        _model=agent.model,
        messages=[],
        api_messages=[],
        api_kwargs={},
        active_system_prompt="system",
        conversation_history=[],
        approx_tokens=0,
        retry_count=0,
        max_retries=3,
        compression_attempts=0,
        api_call_count=0,
    )

    assert verdict.action == "return"
    assert verdict.result["failure_reason"] == "strict_free_policy"
    assert agent.fallback_checks == 0
    assert agent.fallback_attempts == 0


def test_strict_free_copy_does_not_suggest_provider_model_or_paid_fallback():
    classified = classify_api_error(StrictFreeOnlyError("Strict free-only mode rejected request"))

    copy = nonretryable_copy(
        classified,
        provider="openrouter",
        model="openrouter/free",
        summary="Strict free-only mode rejected request",
    ).lower()

    assert "blocked this request before it was sent" in copy
    assert "policy detail:" in copy
    assert "provider said:" not in copy
    assert "another provider" not in copy
    assert "switch" not in copy
    assert "no alternate provider, model, or paid fallback was attempted" in copy


def test_strict_free_error_surface_is_gateway_and_not_retryable():
    surface = build_error_surface_from_result({
        "failed": True,
        "failure_reason": "strict_free_policy",
        "failure_retryable": False,
        "error": "Strict free-only mode rejected request",
    })

    assert surface["layer"] == LAYER_GATEWAY
    assert surface["code"] == "strict_free_policy"
    assert surface["retryable"] is False


def test_deep_research_gateway_copy_does_not_call_policy_rejection_an_outage():
    failure_class, message = TurnRunner._deep_research_failure({
        "failure_reason": "strict_free_policy",
        "error": "Strict free-only mode rejected request",
    })

    assert failure_class == "strict_free_policy"
    assert "blocked this request before it was sent" in message
    assert "temporarily unavailable" not in message
    assert "switch" not in message.lower()
    assert "no alternate provider, model, or paid fallback was attempted" in message.lower()
