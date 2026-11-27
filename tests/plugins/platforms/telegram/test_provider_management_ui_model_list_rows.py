"""Control-row order on a per-provider model list in the Telegram
provider-management UI.

Rows below the model grid and pager: Search models (+ Recent when the
provider has recent models), Make default for all + Refresh live catalog,
Filter (beta, when on), Back + Close last. Clear search, Provider and
All models are gone from this screen.
"""

import pytest

from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from tests.plugins.platforms.telegram.test_provider_management_ui import (
    CallbackEvent, FakeAdapter, Source, _provider,
)


def _models_view_button_rows(markup):
    """(label, callback data) pairs per keyboard row, in render order."""
    return [
        [(button.text, button.type.data.decode("utf-8")) for button in row.buttons]
        for row in markup.rows
    ]


def test_models_view_bottom_rows_are_search_make_default_back_close():
    """Per-provider model list control rows: Search, Make default + Refresh,
    Back + Close last. Clear search, Provider and All models are gone; the
    model grid and pager above are untouched."""
    ui = TelegramProviderManagementUI(FakeAdapter())
    models = [f"vendor/model-{i:02}" for i in range(11)]
    state = ui._new_state(
        Source(), [_provider(models=models, total_models=len(models))],
        current_model=models[0], current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider="openrouter",
    )
    _, markup = ui._render(state)
    rows = _models_view_button_rows(markup)
    labels = [[label for label, _ in row] for row in rows]
    search_index = next(i for i, row in enumerate(labels) if "Search models" in row)
    # Model grid (two-per-row select buttons) then the pager, untouched.
    assert labels[search_index - 1] == ["Next ›"]
    assert all(":select:" in data for row in rows[:search_index - 1] for _, data in row)
    assert labels[search_index:] == [
        ["Search models"],
        ["Make default for all", "Refresh live catalog"],
        ["Back", "Close"],
    ]
    assert not {"Clear search", "Provider", "All models"} & {
        label for row in labels for label in row}


@pytest.mark.asyncio
async def test_models_view_search_row_keeps_recent_without_clear_search(tmp_path):
    """With recent models for the provider, the search row is Search models +
    Recent — Recent still toggles the recents-only list; Clear search is gone."""
    (tmp_path / "config.yaml").write_text(
        "_recent_models:\n  openrouter:\n    - vendor/model-b\n")
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider(models=["vendor/model-a", "vendor/model-b"], total_models=2)],
        current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=tmp_path, view="models", selected_provider="openrouter",
    )
    state["message_id"] = "55"
    _, markup = ui._render(state)
    rows = _models_view_button_rows(markup)
    labels = [[label for label, _ in row] for row in rows]
    search_row = next(row for row in rows
                      if any(label == "Search models" for label, _ in row))
    assert [label for label, _ in search_row] == ["Search models", "Recent"]
    recent_data = next(data for label, data in search_row if label == "Recent")
    assert recent_data.endswith(":recent-models")
    assert not {"Clear search", "Provider", "All models"} & {
        label for row in labels for label in row}
    assert await ui.handle_callback(CallbackEvent(55), Source(), recent_data)
    assert state.get("recent_models_only") is True
