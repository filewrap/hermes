"""Shared-provider filter on the Telegram providers screen.

The bottom custom row reads ``add custom`` + ``custom • N``; a ``shared``
button joins it (third, right) only when at least one provider is shared
with all. Tapping it filters the list to shared providers, like the
custom filter.
"""

import pytest

from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from tests.plugins.platforms.telegram.test_provider_management_ui import (
    CallbackEvent, FakeAdapter, Source, _button_rows, _button_texts, _provider, _state,
)


def _share_provider(monkeypatch, tmp_path, slug):
    """Mark one provider shared with all, as its adder (user 123)."""
    from agent.credential_pool import PooledCredential, load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool = load_pool(slug)
    entry = PooledCredential(
        provider=slug, id="abc123", label="Credential 1", auth_type="api_key",
        priority=0, source="manual", access_token="test-token",
    )
    entry.extra["added_by"] = "123"
    entry.extra["shared"] = True
    pool.add_entry(entry)


def _custom_row_labels(markup):
    rows = _button_rows(markup)
    return next(row for row in rows if "add custom" in row)


@pytest.mark.asyncio
async def test_shared_button_shows_when_a_provider_is_shared(tmp_path, monkeypatch):
    """The shared filter sits in the custom row, after custom, when at
    least one provider is shared with all."""
    _share_provider(monkeypatch, tmp_path, "openrouter")
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    assert await ui.open(
        source, [_provider(), _provider(slug="nous", name="Nous Portal",
                                         is_current=False, is_profile_default=False)],
        current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=tmp_path,
    )
    state = _state(ui)
    _text, markup = ui._render(state)
    assert _custom_row_labels(markup) == ["add custom", "custom • 0", "shared"]


@pytest.mark.asyncio
async def test_shared_button_hidden_when_nothing_is_shared(tmp_path):
    """No shared provider, no shared button: the custom row stays two wide."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    assert await ui.open(
        source, [_provider()], current_model="vendor/model-a",
        current_provider="openrouter", session_key="s", profile_home=tmp_path,
    )
    state = _state(ui)
    _text, markup = ui._render(state)
    assert _custom_row_labels(markup) == ["add custom", "custom • 0"]


@pytest.mark.asyncio
async def test_shared_filter_lists_only_shared_providers(tmp_path, monkeypatch):
    """Tapping shared filters the list to shared providers and resets the
    page to 0, like the custom filter."""
    _share_provider(monkeypatch, tmp_path, "openrouter")
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    assert await ui.open(
        source, [_provider(), _provider(slug="nous", name="Nous Portal",
                                         is_current=False, is_profile_default=False)],
        current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=tmp_path,
    )
    state = _state(ui)
    state["page"] = 1
    assert await ui.handle_callback(
        CallbackEvent(55), source, f"pv:{state['id']}:filter:shared")
    state = _state(ui)
    assert state["filter"] == "shared"
    assert state["page"] == 0
    labels = _button_texts(ui._render(state)[1])
    assert "Current · OpenRouter · Added" in labels
    assert "Nous Portal · Added" not in labels
    assert _custom_row_labels(ui._render(state)[1]) == ["add custom", "custom • 0", "shared"]
