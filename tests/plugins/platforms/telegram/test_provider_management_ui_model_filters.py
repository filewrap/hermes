"""Model-picker filters in the Telegram provider-management models list.

Beta (platforms.telegram.model_picker_filters, off by default): a Filter
button at the bottom of one provider's model list opens Free + context
bucket buttons; tapping one narrows the list in place, scoped to the open
provider only. Tapping it again (or Clear filter) restores the full list.
"""

from types import SimpleNamespace

import pytest

from plugins.platforms.telegram.model_picker_filters import bucket_for_context
from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from tests.plugins.platforms.telegram.test_provider_management_ui import (
    CallbackEvent, FakeAdapter, Source, _provider,
)


def _buttons(markup):
    return [button for row in markup.rows for button in row.buttons]


def _button_rows(markup):
    """Button labels per keyboard row, in render order."""
    return [[button.text for button in row.buttons] for row in markup.rows]


def _button_with_suffix(markup, suffix):
    return next(
        button for button in _buttons(markup)
        if button.type.data.decode("utf-8").endswith(suffix)
    )


def _select_indices(markup):
    return sorted(
        int(button.type.data.decode("utf-8").rsplit(":select:", 1)[1])
        for button in _buttons(markup)
        if ":select:" in button.type.data.decode("utf-8")
    )


def _beta_on_home(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "platforms:\n  telegram:\n    model_picker_filters: true\n"
    )
    return tmp_path


def _models_state(ui, providers, *, profile_home=None, selected_provider="openrouter"):
    state = ui._new_state(
        Source(), providers, current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=profile_home, view="models",
        selected_provider=selected_provider,
    )
    # Bind the card to the callback event's message, like bind_prepared_model_catalog_state
    # does for a sent card; otherwise _source_for_callback refuses every tap.
    state["message_id"] = "55"
    return state


def test_filter_button_absent_when_beta_off():
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    providers = [_provider(slug="openrouter", models=["vendor/model-a", "vendor/model-b"],
                           total_models=2, is_current=True, default_model="vendor/model-a")]
    state = _models_state(ui, providers, profile_home=None)
    _text, markup = ui._render(state)
    assert not [b for b in _buttons(markup) if b.text == "Filter"]
    assert _select_indices(markup) == [0, 1]


@pytest.mark.asyncio
async def test_filter_button_opens_panel_with_free_and_context_buckets(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    providers = [_provider(slug="openrouter", models=["vendor/model-a", "vendor/model-b"],
                           total_models=2, is_current=True, default_model="vendor/model-a")]
    state = _models_state(ui, providers, profile_home=_beta_on_home(tmp_path))
    _text, markup = ui._render(state)
    filter_button = _button_with_suffix(markup, ":model-filter")
    assert filter_button.text == "Filter"
    assert not [b for b in _buttons(markup) if b.text in {"Free", "256k", "512k", "512k+", "1M", "2M"}]

    assert await ui.handle_callback(CallbackEvent(55), Source(), f"pv:{state['id']}:model-filter")
    _text, markup = ui._render(state)
    assert _button_with_suffix(markup, ":model-filter").style.bg_primary is True
    for label, key in (("Free", "free"), ("256k", "ctx-256k"), ("512k", "ctx-512k"),
                       ("512k+", "ctx-512k-plus"), ("1M", "ctx-1m"), ("2M", "ctx-2m")):
        button = _button_with_suffix(markup, f":model-filter-value:{key}")
        assert button.text == label
        assert button.style is None or button.style.bg_primary is not True


@pytest.mark.asyncio
async def test_filter_row_sits_above_back_close(tmp_path):
    """Filter button (and its open panel) sit between the Make default +
    Refresh row and the Back + Close row, which stays last."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    providers = [_provider(slug="openrouter", models=["vendor/model-a", "vendor/model-b"],
                           total_models=2, is_current=True, default_model="vendor/model-a")]
    state = _models_state(ui, providers, profile_home=_beta_on_home(tmp_path))
    _text, markup = ui._render(state)
    rows = _button_rows(markup)
    filter_index = next(i for i, row in enumerate(rows) if row == ["Filter"])
    assert rows[filter_index - 1] == ["Make default for all", "Refresh live catalog"]
    assert rows[filter_index + 1] == ["Back", "Close"]

    # Open the panel: the option rows land under Filter, still above Back + Close.
    assert await ui.handle_callback(CallbackEvent(55), Source(), f"pv:{state['id']}:model-filter")
    _text, markup = ui._render(state)
    rows = _button_rows(markup)
    filter_index = next(i for i, row in enumerate(rows) if row == ["Filter"])
    assert rows[-1] == ["Back", "Close"]
    assert any("Free" in row for row in rows[filter_index + 1:-1])


@pytest.mark.asyncio
async def test_free_filter_shows_only_free_models_and_toggles_back(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    model_ids = ["vendor/model-a:free", "vendor/model-b", "vendor/model-c-free",
                 "vendor/model-d", "vendor/model-e_free"]
    providers = [_provider(slug="openrouter", models=model_ids, total_models=5,
                           is_current=True, default_model=model_ids[1])]
    state = _models_state(ui, providers, profile_home=_beta_on_home(tmp_path))
    source = Source()

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:model-filter")
    assert await ui.handle_callback(
        CallbackEvent(55), source, f"pv:{state['id']}:model-filter-value:free")
    text, markup = ui._render(state)
    assert "- Filter: Free" in text
    assert _select_indices(markup) == [0, 2, 4]
    free_button = _button_with_suffix(markup, ":model-filter-value:free")
    assert free_button.style.bg_primary is True
    assert _button_with_suffix(markup, ":model-filter").text == "Filter: Free"
    assert _button_with_suffix(markup, ":model-filter-value:clear").text == "Clear filter"

    # Tapping the active filter again restores the full list.
    assert await ui.handle_callback(
        CallbackEvent(55), source, f"pv:{state['id']}:model-filter-value:free")
    text, markup = ui._render(state)
    assert "- Filter: Free" not in text
    assert _select_indices(markup) == [0, 1, 2, 3, 4]

    # Clear filter restores the full list too.
    assert await ui.handle_callback(
        CallbackEvent(55), source, f"pv:{state['id']}:model-filter-value:free")
    assert await ui.handle_callback(
        CallbackEvent(55), source, f"pv:{state['id']}:model-filter-value:clear")
    _text, markup = ui._render(state)
    assert _select_indices(markup) == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("context,expected", [
    (None, None), (0, None), (128_000, "ctx-256k"), (262_144, "ctx-256k"),
    (262_145, "ctx-512k"), (524_288, "ctx-512k"), (524_289, "ctx-512k-plus"),
    (1_048_575, "ctx-512k-plus"), (1_048_576, "ctx-1m"), (2_097_151, "ctx-1m"),
    (2_097_152, "ctx-2m"), (8_000_000, "ctx-2m"),
])
def test_context_buckets_partition_the_window_space(context, expected):
    assert bucket_for_context(context) == expected


@pytest.mark.asyncio
async def test_context_bucket_filter_uses_models_dev_windows(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    model_ids = ["vendor/small", "vendor/medium", "vendor/large", "vendor/huge",
                 "vendor/unknown"]
    contexts = {
        "vendor/small": 200_000, "vendor/medium": 700_000, "vendor/large": 1_500_000,
        "vendor/huge": 3_000_000, "vendor/unknown": None,
    }
    monkeypatch.setattr(
        "agent.models_dev.lookup_models_dev_context",
        lambda provider, model, allow_network=False: contexts[model],
    )
    providers = [_provider(slug="openrouter", models=model_ids, total_models=5,
                           is_current=True, default_model=model_ids[0])]
    state = _models_state(ui, providers, profile_home=_beta_on_home(tmp_path))
    source = Source()

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:model-filter")
    assert await ui.handle_callback(
        CallbackEvent(55), source, f"pv:{state['id']}:model-filter-value:ctx-512k-plus")
    text, markup = ui._render(state)
    assert "- Filter: 512k+ context" in text
    assert _select_indices(markup) == [1]

    # An unknown window matches no bucket.
    assert await ui.handle_callback(
        CallbackEvent(55), source, f"pv:{state['id']}:model-filter-value:ctx-2m")
    text, markup = ui._render(state)
    assert _select_indices(markup) == [3]
    assert "No models match the filter." not in text

    assert await ui.handle_callback(
        CallbackEvent(55), source, f"pv:{state['id']}:model-filter-value:ctx-256k")
    _text, markup = ui._render(state)
    assert _select_indices(markup) == [0]


@pytest.mark.asyncio
async def test_filter_is_bound_to_the_open_provider(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    providers = [
        _provider(slug="openrouter", models=["vendor/model-a:free", "vendor/model-b"],
                  total_models=2, is_current=True, default_model="vendor/model-a"),
        _provider(slug="other", name="Other", models=["other/model-a:free", "other/model-b"],
                  total_models=2, is_current=False),
    ]
    state = _models_state(ui, providers, profile_home=_beta_on_home(tmp_path))
    source = Source()

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:model-filter")
    assert await ui.handle_callback(
        CallbackEvent(55), source, f"pv:{state['id']}:model-filter-value:free")
    _text, markup = ui._render(state)
    assert _select_indices(markup) == [0]

    # Opening another provider drops the filter: its full list shows.
    state.update(view="models", selected_provider="other")
    text, markup = ui._render(state)
    assert "- Filter:" not in text
    assert _select_indices(markup) == [0, 1]


def test_settings_row_shows_model_picker_filters_with_beta_badge(tmp_path):
    from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

    adapter = FakeAdapter()
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name
    text, _markup = ui._home_view(state)
    assert ('<td><b>Model picker filters</b> <tg-button type="callback_data" style="primary" '
            'data="badge:beta">beta</tg-button></td><td>Off</td>') in text
    assert "model-picker-filters-toggle" in text


@pytest.mark.asyncio
async def test_settings_toggle_flips_model_picker_filters_config(tmp_path):
    from hermes_cli.config import read_user_config_raw
    from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

    adapter = FakeAdapter()
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name

    event = CallbackEvent(55)
    assert await ui.handle_callback(event, source, f"ps:{state['id']}:model-picker-filters-toggle")
    assert read_user_config_raw(tmp_path / "config.yaml")["platforms"]["telegram"]["model_picker_filters"] is True
    text, _markup = ui._home_view(state)
    assert ('<td><b>Model picker filters</b> <tg-button type="callback_data" style="primary" '
            'data="badge:beta">beta</tg-button></td><td>On</td>') in text
