from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _buttons(markup):
    return [getattr(item, "button", item) for row in markup.rows for item in row.buttons]


def _rich_text(request):
    rich = request.rich_message
    return getattr(rich, "html", None) or getattr(rich, "markdown", None) or getattr(request, "message", "")


def test_provider_page_renders_native_rich_hierarchy_inventory_and_clamped_pages():
    adapter = TelethonAdapter(PlatformConfig())
    providers = [
        {"slug": f"provider-{i}", "name": f"Provider {i}"}
        for i in range(17)
    ]
    state = {
        "providers": providers,
        "current_model": "vendor/current-model",
        "current_provider": "provider-8",
    }

    text, markup = adapter._model_picker_provider_page(12, state, 10_000)

    assert state["stage"] == "provider"
    assert state["page"] == 2
    assert text.startswith(
        "# Model configuration\n\n---\n\n## Current configuration\n"
    )
    assert "- **Model:** **`vendor/current-model`**" in text
    assert "- **Provider:** **provider-8**" in text
    assert "## Providers\nChoose a provider to browse its available models." in text
    assert "- Provider 16" in text
    assert "Page 3 of 3 • total 17" in text
    buttons = _buttons(markup)
    assert [button.text for button in buttons] == ["Provider 16", "‹ Previous", "Cancel"]

    text, _ = adapter._model_picker_provider_page(12, state, -50)
    assert state["page"] == 0
    assert "Page 1 of 3 • total 17" in text
    assert "Provider 0" in text


def test_model_page_renders_current_value_and_inventory_with_clamped_pages():
    adapter = TelethonAdapter(PlatformConfig())
    models = [f"vendor/model-{i}" for i in range(21)]
    state = {
        "model_list": models,
        "selected_provider": "vendor",
        "selected_provider_name": "Vendor",
        "current_model": "vendor/model-20",
    }

    text, markup = adapter._model_picker_model_page(19, state, 50)

    assert state["stage"] == "model"
    assert state["page"] == 2
    assert text.startswith(
        "# Model configuration\n\n---\n\n## Current configuration\n"
    )
    assert "- **Model:** **`vendor/model-20`**" in text
    assert "- **Provider:** **Vendor**" in text
    assert "## Models for Vendor" in text
    assert "- **`vendor/model-20`** — Current model" in text
    assert "Page 3 of 3 • total 21" in text
    current_button = next(button for button in _buttons(markup) if button.text == "model-20")
    assert current_button.style.bg_success is True

    text, _ = adapter._model_picker_model_page(19, state, -1)
    assert state["page"] == 0
    assert "Page 1 of 3 • total 21" in text
    assert "`vendor/model-0`" in text


def test_empty_model_inventory_has_one_page_and_explicit_empty_state():
    adapter = TelethonAdapter(PlatformConfig())
    text, markup = adapter._model_picker_model_page(
        1,
        {"model_list": [], "selected_provider_name": "Vendor", "current_model": "old"},
        8,
    )

    assert "No models are available for this provider." in text
    assert "Page 1 of 1 • total 0" in text
    assert [button.text for button in _buttons(markup)] == ["‹ Providers", "Cancel"]


@pytest.mark.asyncio
async def test_send_model_picker_uses_native_rich_message_with_picker_layout():
    adapter = TelethonAdapter(PlatformConfig())
    requests = []

    class Client:
        async def __call__(self, request):
            requests.append(request)
            return SimpleNamespace(id=101)

    adapter._client = Client()
    result = await adapter.send_model_picker(
        chat_id="42",
        providers=[{"slug": "vendor", "name": "Vendor", "models": ["vendor/model-a"]}],
        current_model="vendor/model-b",
        current_provider="vendor",
        session_key="session",
        on_model_selected=AsyncMock(),
        metadata={"user_id": "7"},
    )

    assert result.success
    request = requests[0]
    assert type(request).__name__ == "SendMessageRequest"
    assert type(request.rich_message).__name__ == "InputRichMessage"
    markdown = _rich_text(request)
    assert "# Model configuration" in markdown
    assert "## Current configuration" in markdown
    assert "## Providers" in markdown
    assert "- **Provider:** **vendor**" in markdown
    assert "Page 1 of 1 • total 1" in markdown
    assert request.reply_markup.rows
