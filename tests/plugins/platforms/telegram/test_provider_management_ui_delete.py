"""Delete-failure behaviour of the Telegram provider-management UI.

Split out of test_provider_management_ui.py: that file is over the
FILE_LINES target, so new delete tests live here instead.
"""

from __future__ import annotations

import json

import pytest
import yaml

from tests.plugins.platforms.telegram.test_provider_management_ui import (
    CallbackEvent,
    FakeAdapter,
    Source,
    TelegramProviderManagementUI,
    _provider,
    _rich_text,
    _state,
    _switch_runner,
)


@pytest.mark.asyncio
async def test_delete_custom_provider_failure_names_the_reason(tmp_path, monkeypatch):
    """A delete that cannot resolve its row must say WHY on the card: the
    bare "(ValueError)" notice hid the real reason from the user and the host
    log alike, so a provider that could not be deleted looked like a silent
    no-op."""
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "vendor/model-b", "provider": "nous"},
    }), encoding="utf-8")
    (tmp_path / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    stores = {"nous": [{
        "id": "nous-1", "label": "Credential 1", "auth_type": "oauth",
        "source": "device_code", "active": True,
    }]}
    adapter = FakeAdapter()
    adapter.gateway_runner = _switch_runner(stored_session_overrides={}, session_overrides={})
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda provider: list(stores.get(provider["slug"], [])))
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda provider: bool(stores.get(provider["slug"])))
    source = Source()
    providers = [
        _provider(
            slug="custom:ghost", name="Ghost", is_catalog_provider=False,
            is_user_defined=True, configured=True, is_current=False,
            is_profile_default=False, profile_default_model="",
            models=["ghost-model"], total_models=1, default_model="ghost-model",
        ),
        _provider(
            slug="nous", name="Nous", configured=True, is_current=True,
            is_profile_default=True, profile_default_model="vendor/model-b",
            models=["vendor/model-b"], total_models=1, default_model="vendor/model-b",
        ),
    ]
    await ui.open(source, providers, current_model="vendor/model-b", current_provider="nous",
                  session_key="session-1", profile_home=tmp_path)
    state = _state(ui)
    ghost_index = next(i for i, item in enumerate(state["providers"]) if item["slug"] == "custom:ghost")
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:{ghost_index}")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:delete-ask")
    state = _state(ui)
    assert state["view"] == "delete-confirm"
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:delete-confirm")
    state = _state(ui)
    assert state["view"] == "provider"
    assert "Custom provider not found" in _rich_text(adapter.requests[-1])
