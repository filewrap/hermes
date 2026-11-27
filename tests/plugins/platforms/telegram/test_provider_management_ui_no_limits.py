"""Provider management, no-limits contract: credential removal and the default
pin are open to every admitted user, not just the owner.

Split out of test_provider_management_ui.py so that file stays under its line
cap; the helpers are shared from that module, as
test_provider_management_ui_groups.py already does.
"""

import pytest

from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from tests.plugins.platforms.telegram.test_provider_management_ui import (
    CallbackEvent,
    FakeAdapter,
    Source,
    _provider,
    _rich_text,
    _state,
)


@pytest.mark.asyncio
async def test_allowed_user_can_remove_credential_and_pin_default(tmp_path, monkeypatch):
    """No-limits: credential removal and the default pin are open to every
    admitted user — the buttons render and the actions run, no refusal."""
    from hermes_cli import auth_commands

    removed = []
    credential_store = [{
        "id": "abc123", "label": "Credential 1", "auth_type": "api_key",
        "source": "manual", "active": True,
    }]

    def remove_credential(provider, target):
        removed.append((provider, target))
        credential_store.clear()
        return {"removed": True}

    monkeypatch.setattr(auth_commands, "remove_credential_for_ui", remove_credential)
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=222\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: list(credential_store))
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: bool(credential_store))
    source = Source(user_id="999")
    await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="session-1", profile_home=tmp_path,
        view="provider", selected_provider="openrouter",
    )
    state = _state(ui)
    _text, markup = ui._render(state)
    assert "Make default for all" in [
        button.text for row in markup.rows for button in row.buttons
    ]

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:remove-ask:0")
    state = _state(ui)
    assert state["view"] == "remove-confirm"
    assert state.get("remove_credential", {}).get("id") == "abc123"
    assert removed == []
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:remove-confirm")
    assert removed == [("openrouter", "abc123")]

    # The pin is attempted, not refused: this harness has no switch runner,
    # so the pin fails safely on the missing route instead of an owner refusal.
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:pin-default-all")
    assert "Only the profile owner can" not in _rich_text(adapter.requests[-1])
    assert "failed safely" in _rich_text(adapter.requests[-1])
