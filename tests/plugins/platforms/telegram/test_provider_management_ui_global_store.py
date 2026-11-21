"""Global-store behaviour of the Telegram provider-management UI.

A custom endpoint added from one profile lands in the launch home's
global store — never the profile's own files — and is readable from every
profile through the union and the pool's global fallback. The UI harness
(``FakeAdapter``, events, ``_provider``, ``_state``) lives in
``test_provider_management_ui.py`` and is imported here, the same way
other test files in this tree share a harness.
"""
import pytest

from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from tests.plugins.platforms.telegram.test_provider_management_ui import (
    CallbackEvent, FakeAdapter, IncomingEvent, Source, _provider, _state,
)


@pytest.mark.asyncio
async def test_custom_endpoint_real_config_and_credential_storage_are_global(tmp_path, monkeypatch):
    """A custom endpoint added from one profile lands in the global store:
    the launch home's config.yaml + credential store — never the profile's
    own files — and is readable from every profile through the union and
    the pool's global fallback."""
    from agent.credential_pool import load_pool
    from hermes_cli.config import load_config
    from hermes_cli.config_providers import global_provider_home

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir()
    profile_b.mkdir()
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_fetch_custom_model_ids", lambda _data, _key: ["profile-live-model"])
    custom = _provider(slug="custom", name="Custom", auth_type="virtual", api_key_env_vars=[], is_user_defined=True)
    source = Source()
    await ui.open(source, [custom], current_model="", current_provider="openrouter",
                  session_key="s", profile_home=profile_a)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:custom-create")
    await ui.handle_message(IncomingEvent(81), Source(message_id="81"), "my endpoint")
    await ui.handle_message(IncomingEvent(82), Source(message_id="82"), "https://api.example.test/v1")
    secret = "sk-test-global-123"
    await ui.handle_message(IncomingEvent(83), Source(message_id="83"), secret)

    # The definition + key live in the global store, not in either profile.
    launch = global_provider_home()
    global_config = load_config()
    assert global_config["custom_providers"][0]["name"] == "my endpoint"
    assert global_config["custom_providers"][0]["models"] == {"profile-live-model": {}}
    assert secret not in (launch / "config.yaml").read_text(encoding="utf-8")
    for profile in (profile_a, profile_b):
        config_path = profile / "config.yaml"
        assert not config_path.exists() or "my endpoint" not in config_path.read_text(encoding="utf-8")

    with ui._profile_scope({"profile_home": str(profile_a)}):
        config_a = load_config()
        pool_a = load_pool("custom:my-endpoint").entries()
    assert not config_a.get("custom_providers")
    assert len(pool_a) == 1 and pool_a[0].access_token == secret

    # ... and the same key is readable from the other profile too.
    with ui._profile_scope({"profile_home": str(profile_b)}):
        config_b = load_config()
        pool_b = load_pool("custom:my-endpoint").entries()
    assert not config_b.get("custom_providers")
    assert len(pool_b) == 1 and pool_b[0].access_token == secret
