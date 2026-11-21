"""Free-model markers in the Telegram provider-management models list.

A model id carrying :free, -free or _free reads green, and the Catalog
notice counts how many of the matching models are free.
"""

from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from tests.plugins.platforms.telegram.test_provider_management_ui import (
    FakeAdapter, Source, _provider,
)


def test_free_models_are_green_and_selection_markers_remain_distinct():
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    free_ids = ["vendor/model-a:free", "vendor/model-b:free", "vendor/model-c",
                "vendor/model-d-free", "vendor/model-e_free"]
    providers = [
        _provider(slug="openrouter", models=free_ids, total_models=5, is_current=True,
                  default_model=free_ids[0]),
        _provider(slug="other", name="Other", models=["other/model"], total_models=1,
                  is_current=False),
    ]
    state = ui._new_state(
        Source(), providers, current_model=free_ids[0], current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider=None,
    )
    catalogs, provider_markup = ui._render(state)
    catalog_buttons = [button for row in provider_markup.rows for button in row.buttons]
    other_button = next(button for button in catalog_buttons if button.type.data.decode().endswith(":models:1"))
    current_button = next(button for button in catalog_buttons if button.type.data.decode().endswith(":models:0"))
    assert "Other" in other_button.text
    assert other_button.style.bg_primary is not True
    assert "Current · OpenRouter" in current_button.text
    assert current_button.style.bg_success is True
    assert catalogs.count("vendor/model-a:free") >= 1
    assert "Green marks :free models" not in catalogs

    state.update(view="providers", selected_provider="other")
    _provider_picker_text, provider_picker_markup = ui._render(state)
    selected_provider_button = next(
        button for row in provider_picker_markup.rows for button in row.buttons
        if button.type.data.decode("utf-8").endswith(":provider:1")
    )
    assert "Selected · Other" in (selected_provider_button.text or "")
    # Provider names are regular keyboard buttons now; the selected one reads
    # blue, marked by its "Selected · " prefix.
    assert selected_provider_button.style.bg_primary is True

    state.update(view="models", selected_provider="openrouter")
    model_text, model_markup = ui._render(state)
    assert "vendor/model-a:free" in model_text
    assert "Green marks the current model and free models" in model_text
    assert "4 models are free" in model_text
    model_buttons = [button for row in model_markup.rows for button in row.buttons]
    selected_free = next(button for button in model_buttons if button.type.data.decode().endswith(":select:0"))
    free_button = next(button for button in model_buttons if button.type.data.decode().endswith(":select:1"))
    assert selected_free.text.startswith("Current ·") and selected_free.style.bg_success is True
    assert free_button.style.bg_success is True
    dash_free = next(button for button in model_buttons if button.type.data.decode().endswith(":select:3"))
    underscore_free = next(button for button in model_buttons if button.type.data.decode().endswith(":select:4"))
    assert dash_free.style.bg_success is True
    assert underscore_free.style.bg_success is True
    paid_button = next(button for button in model_buttons if button.type.data.decode().endswith(":select:2"))
    assert paid_button.style is None or paid_button.style.bg_success is not True


def test_free_count_counts_the_matching_search_not_the_whole_catalog():
    """The free count line counts free models among the search matches; a
    search matching one free model reads '1 model is free'."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    model_ids = ["vendor/model-a:free", "vendor/model-b-free",
                 "vendor/model-c_free", "vendor/model-d"]
    providers = [
        _provider(slug="openrouter", models=model_ids, total_models=4, is_current=True,
                  default_model=model_ids[3]),
    ]
    state = ui._new_state(
        Source(), providers, current_model="vendor/elsewhere", current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider="openrouter",
    )
    model_text, model_markup = ui._render(state)
    assert "3 models are free" in model_text
    model_buttons = [button for row in model_markup.rows for button in row.buttons]
    for index in (0, 1, 2):
        button = next(b for b in model_buttons if b.type.data.decode().endswith(f":select:{index}"))
        assert button.style.bg_success is True
    paid_button = next(b for b in model_buttons if b.type.data.decode().endswith(":select:3"))
    assert paid_button.style is None or paid_button.style.bg_success is not True

    state["search_query"] = "model-b"
    searched_text, searched_markup = ui._render(state)
    assert "1 model is free" in searched_text
    assert "3 models are free" not in searched_text
    searched_buttons = [button for row in searched_markup.rows for button in row.buttons]
    matched = next(b for b in searched_buttons if b.type.data.decode().endswith(":select:1"))
    assert matched.style.bg_success is True
