from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.run_turn import GatewayTurnMixin


class _Runner(GatewayTurnMixin):
    def __init__(self, override=None):
        conversation = SimpleNamespace(model_override=override, last_resolved_model="")
        self.state = SimpleNamespace(conversation=conversation)

    def _resolve_session_key_or_none(self, _source, _session_key):
        return "dm-session"

    def _rehydrate_session_model_override(self, _session_key):
        return None

    def _peek_session_state(self, _session_key):
        return self.state

    def _sessions_map(self):
        return {"dm-session": self.state}

    def _session_state(self, _session_key):
        return self.state


def _source(preferences):
    return SimpleNamespace(
        platform="telegram", chat_id="77", thread_id=None, parent_chat_id=None,
        _telegram_user_settings=preferences,
    )


def _resolve(runner, source, monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda _config: "profile-model")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {
        "provider": "profile-provider", "model": None, "api_key": "profile-key",
    })
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs_for_provider", lambda provider, target_model=None: {
        "provider": provider, "model": "provider-bundled-model", "api_key": f"{provider}-key",
    })
    monkeypatch.setattr(gateway_run, "_credential_pool_for_provider", lambda _provider: None)
    return runner._resolve_session_agent_runtime(source=source, user_config={})


def test_personal_provider_default_uses_its_bundled_model(monkeypatch):
    runner = _Runner()
    model, runtime = _resolve(runner, _source({"default_provider": "personal-provider"}), monkeypatch)

    assert model == "provider-bundled-model"
    assert runtime["provider"] == "personal-provider"
    assert runtime["api_key"] == "personal-provider-key"


def test_personal_model_and_provider_defaults_apply_to_that_user(monkeypatch):
    runner = _Runner()
    model, runtime = _resolve(runner, _source({
        "default_provider": "personal-provider", "default_model": "chosen-model",
    }), monkeypatch)

    assert model == "chosen-model"
    assert runtime["provider"] == "personal-provider"


def test_explicit_session_model_override_has_precedence_over_personal_defaults(monkeypatch):
    runner = _Runner({
        "model": "session-model", "provider": "session-provider", "api_key": "session-key",
    })
    source = _source({"default_provider": "personal-provider", "default_model": "chosen-model"})

    model, runtime = _resolve(runner, source, monkeypatch)

    assert model == "session-model"
    assert runtime["provider"] == "session-provider"
    assert runtime["api_key"] == "session-key"
