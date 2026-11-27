"""Providers/models search auto-clear (live bug 2026-11-25).

The user searched "free", selected a model, then pressed Back twice — and
the search stayed active, the provider list still filtered to the 2 matches.
A search is spent once the user leaves the search context: Back (both the
history-restore path and the no-history fallback), Close, and a model
select all drop the query and the live pool. The models view also offers
a Clear search button while a search is active, like the providers view
already does.
"""

from __future__ import annotations

import pytest

from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from tests.plugins.platforms.telegram.test_provider_management_ui import (
    CallbackEvent, FakeAdapter, Source, _provider, _state,
)


@pytest.mark.asyncio
async def test_back_after_search_clears_search_and_unfilters_list(tmp_path, monkeypatch):
    """A search is spent once the user leaves the search context: Back
    (both the history-restore path and the no-history fallback) drops the
    query and the live pool, so the list comes back unfiltered."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    source = Source()
    providers = [
        _provider(slug="nous", name="Nous Research", is_current=True,
                  models=["tencent/hy4-flash:free", "vendor/paid"],
                  default_model="tencent/hy4-flash:free", is_profile_default=True),
        _provider(slug="opencode", name="OpenCode", is_current=False,
                  models=["vendor/model-c"], default_model="vendor/model-c",
                  is_profile_default=False),
    ]
    await ui.open(source, providers, current_model="tencent/hy4-flash:free", current_provider="nous",
                  session_key="s", profile_home=tmp_path, view="providers")
    state = _state(ui)

    def _search_free():
        state["search_query"] = "free"
        state["live_search_models"] = ["tencent/hy4-flash:free"]
        state["live_search_provider"] = "nous"

    # History-restore path: search, open the provider, Back.
    _search_free()
    assert len(ui._visible_providers(state)) == 1
    ui._push_history(state)
    state.update(view="provider", selected_provider="nous")
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:back")
    state = _state(ui)
    assert state["view"] == "providers"
    assert state["search_query"] == ""
    assert "live_search_models" not in state
    assert "live_search_provider" not in state
    assert len(ui._visible_providers(state)) == 2

    # No-history fallback: search again, no frame to restore, Back.
    _search_free()
    assert len(ui._visible_providers(state)) == 1
    state["history"] = []
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:back")
    state = _state(ui)
    assert state["view"] == "providers"
    assert state["search_query"] == ""
    assert "live_search_models" not in state
    assert "live_search_provider" not in state
    assert len(ui._visible_providers(state)) == 2


@pytest.mark.asyncio
async def test_model_select_from_search_clears_search_and_models_offer_clear(tmp_path, monkeypatch):
    """Selecting a model spends the search, and the models view offers a
    Clear search button while a search is active (the providers view
    already does)."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    async def select(_state, _source, model, provider):
        _state["current_model"] = model
        _state["current_provider"] = provider
        return f"Saved {provider}/{model}"
    monkeypatch.setattr(ui, "_select_model", select)
    source = Source()
    await ui.open(source, [_provider(slug="nous", name="Nous Research", is_current=True,
                                     models=["tencent/hy4-flash:free", "vendor/paid"],
                                     default_model="tencent/hy4-flash:free")],
                  current_model="tencent/hy4-flash:free", current_provider="nous",
                  session_key="s", profile_home=tmp_path, view="models", selected_provider="nous")
    state = _state(ui)
    state["search_query"] = "free"
    state["live_search_models"] = ["tencent/hy4-flash:free"]
    state["live_search_provider"] = "nous"
    _text, markup = ui._render(state)
    assert "Clear search" in [button.text for row in markup.rows for button in row.buttons]
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:select:0")
    state = _state(ui)
    assert state["current_model"] == "tencent/hy4-flash:free"
    assert state["search_query"] == ""
    assert "live_search_models" not in state
    assert "live_search_provider" not in state
