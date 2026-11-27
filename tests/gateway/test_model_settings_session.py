"""Conversation-scoped /msettings overrides are layered into API request kwargs."""
from gateway.run_agent_cache import GatewayAgentCacheMixin


class _Runner(GatewayAgentCacheMixin):
    def __init__(self):
        self._sessions = {}

    def _session_state(self, key):
        from gateway.session_state import SessionState
        return self._sessions.setdefault(key, SessionState())

    def _peek_session_state(self, key):
        return self._sessions.get(key)


def test_model_settings_round_trip_between_conversations_and_new_session_reset():
    runner = _Runner()
    runtime_a = {"provider": "openrouter", "request_overrides": {"top_p": 0.9, "vendor": "keep"}}
    runtime_b = {"provider": "openrouter", "request_overrides": {"temperature": 0.2}}

    runner._session_state("A").conversation.model_settings.update(
        {"temperature": 0.8, "max_tokens": 4096})
    runner._session_state("B").conversation.model_settings.update(
        {"temperature": 0.3, "frequency_penalty": -0.5})

    model_a1, effective_a1 = runner._apply_session_model_override("A", "model-a", dict(runtime_a))
    _model_b, effective_b = runner._apply_session_model_override("B", "model-b", dict(runtime_b))
    model_a2, effective_a2 = runner._apply_session_model_override("A", "model-a", dict(runtime_a))

    assert model_a1 == model_a2 == "model-a"
    assert effective_a1["request_overrides"] == {"top_p": 0.9, "vendor": "keep",
                                                  "temperature": 0.8, "max_tokens": 4096}
    assert effective_b["request_overrides"] == {"temperature": 0.3, "frequency_penalty": -0.5}
    assert effective_a2["request_overrides"] == effective_a1["request_overrides"]
    assert "frequency_penalty" not in effective_a1["request_overrides"]

    # /new clears the conversation state even when the logical gateway key is reused.
    runner._session_state("A").conversation.clear()
    model_after_new, effective_after_new = runner._apply_session_model_override(
        "A", "model-a", dict(runtime_a))
    assert model_after_new == "model-a"
    assert effective_after_new["request_overrides"] == runtime_a["request_overrides"]
    assert runner._session_state("A").conversation.model_settings == {}


def test_settings_do_not_mutate_provider_runtime_defaults():
    runner = _Runner()
    provider_runtime = {"request_overrides": {"temperature": 0.25, "top_p": 0.7}}
    original = {"request_overrides": dict(provider_runtime["request_overrides"])}
    runner._session_state("A").conversation.model_settings["temperature"] = 1.1

    _, result = runner._apply_session_model_override("A", "model-a", dict(provider_runtime))

    assert result["request_overrides"] == {"temperature": 1.1, "top_p": 0.7}
    assert provider_runtime == original
