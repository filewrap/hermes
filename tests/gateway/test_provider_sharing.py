"""Per-user provider sharing: turn-time enforcement.

The list filter hides unshared providers, but a stale session override or a
profile default can still point at one. A turn for a user who may not use the
resolved provider must fail before it starts — with a clear message — instead
of spending the other user's credential.
"""

from types import SimpleNamespace

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


def _source(user_id):
    return SimpleNamespace(
        platform="telegram", chat_id="77", user_id=user_id, thread_id=None, parent_chat_id=None,
        _telegram_user_settings=None,
    )


def _seed_openrouter_owned_by(tmp_path, monkeypatch, owner):
    from agent.credential_pool import PooledCredential, load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool = load_pool("openrouter")
    entry = PooledCredential(
        provider="openrouter", id="abc123", label="k", auth_type="api_key",
        priority=0, source="manual", access_token="tok",
    )
    entry.extra["added_by"] = owner
    pool.add_entry(entry)


def _patch_resolution(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda _config: "profile-model")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {
        "provider": "openrouter", "model": None, "api_key": "profile-key",
    })
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs_for_provider",
        lambda provider, target_model=None: {
            "provider": provider, "model": "provider-model", "api_key": f"{provider}-key",
        },
    )
    monkeypatch.setattr(gateway_run, "_credential_pool_for_provider", lambda _provider: None)


def test_turn_blocked_for_non_adder_when_provider_unshared(tmp_path, monkeypatch):
    from gateway.provider_sharing import ProviderNotSharedError

    _seed_openrouter_owned_by(tmp_path, monkeypatch, "222")
    _patch_resolution(monkeypatch)
    runner = _Runner()
    with pytest.raises(ProviderNotSharedError) as exc:
        runner._resolve_session_agent_runtime(
            source=_source("111"), user_config={}, enforce_visibility=True,
        )
    assert exc.value.provider == "openrouter"


def test_turn_allowed_for_adder_and_without_enforcement_flag(tmp_path, monkeypatch):
    _seed_openrouter_owned_by(tmp_path, monkeypatch, "222")
    _patch_resolution(monkeypatch)
    runner = _Runner()
    _model, runtime = runner._resolve_session_agent_runtime(
        source=_source("222"), user_config={}, enforce_visibility=True,
    )
    assert runtime["provider"] == "openrouter"
    # Slash-command callers (no enforcement flag) are unaffected.
    _model, runtime = runner._resolve_session_agent_runtime(source=_source("111"), user_config={})
    assert runtime["provider"] == "openrouter"
