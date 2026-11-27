"""Model catalog + dashboard: capabilities, context window, credential status,
and the redesigned providers / catalog / dashboard screens.

Catalog rows carry Added/Missing; the per-provider catalog and the model
dashboard show the current model's capabilities + context window (cached
models.dev data only). The providers list is one short inventory line plus
one hybrid button per row (name, credential status, limited state); the
dashboard drops its stale hedges and its single-page footer; notices render
as blockquotes.
"""

import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent.models_dev import ModelInfo
from gateway.config import Platform
from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from plugins.platforms.telegram.provider_management_ui_text import (
    _model_capabilities_text,
    _provider_button_label,
)


@dataclass(frozen=True)
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    platform: Platform = Platform.TELEGRAM
    message_id: str = "50"
    profile: str | None = None


class CallbackEvent:
    def __init__(self, message_id):
        self.message_id = message_id
        self.answers = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text or "", alert))


class FakeAdapter:
    def __init__(self):
        self._client = object()
        self.requests = []
        self.plain_sends = []
        self.deleted = []
        self.delete_result = True
        self.gateway_runner = None

    def _peer_for(self, chat_id):
        return chat_id

    @staticmethod
    def _sent_message_id(response):
        return str(getattr(response, "id", "")) or None

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=55)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((str(chat_id), str(message_id)))
        return self.delete_result

    async def send(self, *args, **kwargs):
        self.plain_sends.append((args, kwargs))
        return SimpleNamespace(success=True)


def _provider(**updates):
    row = {
        "slug": "openrouter",
        "name": "OpenRouter",
        "description": "Route requests through the OpenRouter API.",
        "auth_type": "api_key",
        "api_key_env_vars": ["OPENROUTER_API_KEY"],
        "base_url_env_var": "OPENROUTER_BASE_URL",
        "signup_url": "https://openrouter.ai/keys",
        "is_catalog_provider": True,
        "configured": True,
        "is_current": True,
        "is_profile_default": True,
        "profile_default_model": "vendor/model-a",
        "models": ["vendor/model-a", "vendor/model-b"],
        "total_models": 2,
        "default_model": "vendor/model-a",
        "default_reasoning": "medium",
    }
    row.update(updates)
    return row


def _keyboard_buttons(markup):
    return [(button.text, button.type.data.decode("utf-8"))
            for row in markup.rows for button in row.buttons]


def _plain_rich_text(value):
    from telethon import types

    if isinstance(value, str):
        return value
    if isinstance(value, types.TextPlain):
        return value.text
    if isinstance(value, types.TextButton):
        return _plain_rich_text(value.text)
    if isinstance(value, types.TextConcat):
        return "".join(_plain_rich_text(child) for child in value.texts)
    if isinstance(value, types.TLObject):
        if hasattr(value, "text"):
            return _plain_rich_text(value.text)
        if hasattr(value, "texts"):
            return "".join(_plain_rich_text(child) for child in value.texts)
        if hasattr(value, "blocks"):
            return "\n".join(_plain_rich_text(child) for child in value.blocks)
    if isinstance(value, (list, tuple)):
        return "\n".join(_plain_rich_text(child) for child in value)
    return ""


def _model_info(**updates):
    info = ModelInfo(
        id="vendor/model-a", name="Model A", family="vendor", provider_id="openrouter",
        reasoning=True, tool_call=True, attachment=True, context_window=262144,
    )
    for key, value in updates.items():
        setattr(info, key, value)
    return info


# ── Catalog rows: credential status ───────────────────────────────────


def test_catalog_row_label_ends_with_credential_status():
    assert _provider_button_label(
        _provider(name="OpenRouter", total_models=65, ready=True, is_current=True)
    ) == "Current · OpenRouter · 65 · Added"
    assert _provider_button_label(
        _provider(name="OpenRouter", total_models=65, ready=False, is_current=False)
    ) == "OpenRouter · 65 · Missing"


def test_catalog_row_label_reports_status_unavailable():
    assert _provider_button_label(
        _provider(name="OpenRouter", total_models=65, ready=False,
                  credential_load_error="boom", is_current=False)
    ) == "OpenRouter · 65 · Status unavailable"


def test_catalog_list_rows_show_added_or_missing():
    ui = TelegramProviderManagementUI(FakeAdapter())
    # The render re-reads the pool; pin the fixture ready flags through the
    # same seams the credential tests use so this stays a rendering test.
    ui._load_credentials = lambda _provider: []
    ui._has_local_auth_material = lambda provider: bool(provider.get("ready"))
    providers = [
        _provider(slug="openrouter", name="OpenRouter", is_current=True, ready=True),
        _provider(slug="nous", name="Nous Portal", is_current=False, ready=False),
    ]
    state = ui._new_state(
        Source(), providers, current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="models",
    )
    _rendered, markup = ui._render(state)
    labels = [label for label, data in _keyboard_buttons(markup) if ":models:" in data]
    assert any(label == "Current · OpenRouter · 2 · Added" for label in labels)
    assert any(label == "Nous Portal · 2 · Missing" for label in labels)


# ── Catalog: capabilities + context window ────────────────────────────


def test_capabilities_text_combines_caps_and_context(monkeypatch):
    monkeypatch.setattr(
        "agent.models_dev.get_model_info", lambda *args, **kwargs: _model_info())
    assert _model_capabilities_text("openrouter", "vendor/model-a") == (
        "reasoning, tools, vision · 256k context")


def test_capabilities_text_omits_context_when_unknown(monkeypatch):
    monkeypatch.setattr(
        "agent.models_dev.get_model_info",
        lambda *args, **kwargs: _model_info(context_window=0))
    assert _model_capabilities_text("openrouter", "vendor/model-a") == (
        "reasoning, tools, vision")


def test_capabilities_text_is_empty_for_an_unknown_model(monkeypatch):
    monkeypatch.setattr("agent.models_dev.get_model_info", lambda *args, **kwargs: None)
    assert _model_capabilities_text("openrouter", "vendor/nope") == ""


def test_per_provider_catalog_shows_capabilities_for_the_current_model(monkeypatch):
    monkeypatch.setattr(
        "agent.models_dev.get_model_info", lambda *args, **kwargs: _model_info())
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider="openrouter",
    )
    text, _markup = ui._render(state)
    assert "Capabilities: reasoning, tools, vision · 256k context" in text
    assert "Tap a model to make it the default route." in text
    assert "## Catalog" not in text
    assert "## Current values" not in text


def test_per_provider_catalog_omits_capabilities_when_the_model_is_unknown(monkeypatch):
    monkeypatch.setattr("agent.models_dev.get_model_info", lambda *args, **kwargs: None)
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider="openrouter",
    )
    text, _markup = ui._render(state)
    assert "Capabilities:" not in text


def test_per_provider_catalog_shows_search_only_when_searching():
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider="openrouter",
    )
    text, _markup = ui._render(state)
    assert "Search: All model IDs" not in text
    assert "Freshness:" not in text
    assert "freshness not verified" not in text
    state["search_query"] = "model-b"
    text, _markup = ui._render(state)
    assert "Search: model-b" in text


def test_catalogs_list_is_a_short_hint_not_sections():
    ui = TelegramProviderManagementUI(FakeAdapter())
    providers = [_provider(slug=f"p{i}", name=f"Provider {i}", is_current=i == 0)
                 for i in range(3)]
    state = ui._new_state(
        Source(), providers, current_model="vendor/model-a", current_provider="p0",
        session_key="s", profile_home=None, view="models",
    )
    text, _markup = ui._render(state)
    assert "Tap a provider to open its model catalog." in text
    assert "Route: **`p0 / vendor/model-a`**" in text
    assert "Profile-wide default: **" in text
    assert "Providers with models: **3**" in text
    assert "## Current values" not in text
    assert "## Available catalogs" not in text
    assert "Model choices are shown as selectable buttons below." not in text


# ── Providers list: one-line inventory + hybrid limited row ───────────


def test_providers_list_summary_is_one_short_line():
    ui = TelegramProviderManagementUI(FakeAdapter())
    providers = [_provider(slug=f"p{i}", name=f"Provider {i}", is_current=i == 0,
                           is_catalog_provider=i != 8) for i in range(9)]
    state = ui._new_state(
        Source(), providers, current_model="vendor/model", current_provider="p0",
        session_key="s", profile_home=None, view="providers",
    )
    rendered, _markup = ui._render(state)
    assert "<p>9 providers</p>" in rendered
    assert "<table>" not in rendered
    assert "Additionals" not in rendered
    assert "Hint: Use /model to configure model settings." in rendered
    assert "Use /help to see all commands." not in rendered


def test_providers_list_hybrid_row_carries_the_limited_state(monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    now = time.time()
    monkeypatch.setattr(
        text_mod, "provider_cooldown_state",
        lambda slug: {"reset_at": now + 3600, "hit_at": now - 3700} if slug == "nous" else None,
    )
    ui = TelegramProviderManagementUI(FakeAdapter())
    # The render re-reads the pool; pin the fixture ready flags through the
    # same seams the credential tests use so this stays a rendering test.
    ui._load_credentials = lambda _provider: []
    ui._has_local_auth_material = lambda provider: bool(provider.get("ready"))
    providers = [
        _provider(slug="nous", name="Nous Portal", is_current=False, ready=True),
        _provider(slug="openrouter", name="OpenRouter", is_current=True, ready=True),
    ]
    state = ui._new_state(
        Source(), providers, current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="providers",
    )
    _rendered, markup = ui._render(state)
    buttons = _keyboard_buttons(markup)
    assert ("Nous Portal · Added · ⚠ limited · 1h", "pv:1:provider:1") in buttons
    assert ("Current · OpenRouter · Added", "pv:1:provider:0") in buttons
    assert not any(data.startswith("badge:limited") for _label, data in buttons)


@pytest.mark.asyncio
async def test_tapping_a_limited_provider_answers_the_fresh_toast_and_opens_it(
        tmp_path, monkeypatch):
    from plugins.platforms.telegram import provider_management_ui_text as text_mod

    now = time.time()
    monkeypatch.setattr(
        text_mod, "provider_cooldown_state",
        lambda slug: {"reset_at": now + 3700, "hit_at": now - 3700} if slug == "nous" else None,
    )
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    source = Source()
    providers = [
        _provider(slug="nous", name="Nous Portal", is_current=False, ready=True),
        _provider(slug="openrouter", name="OpenRouter", is_current=True, ready=True),
    ]
    await ui.open(source, providers, current_model="vendor/model-a",
                  current_provider="openrouter", session_key="s", profile_home=tmp_path)
    state = next(iter(ui._states.values()))
    _rendered, markup = ui._render(state)
    nous_data = next(
        data for label, data in _keyboard_buttons(markup)
        if label.startswith("Nous Portal"))
    event = CallbackEvent(55)
    assert await ui.handle_callback(event, source, nous_data)
    assert event.answers and event.answers[0][0] == (
        "Rate limit hit 1h 1m ago · resets in 1h 2m.")
    assert event.answers[0][1] is False
    assert next(iter(ui._states.values()))["view"] == "provider"


# ── Notices render as blockquotes ─────────────────────────────────────


def test_notice_renders_as_a_blockquote():
    from telethon import types

    ui = TelegramProviderManagementUI(FakeAdapter())
    block = ui._notice_block("hello `code` world")
    assert isinstance(block, types.PageBlockBlockquote)
    assert "Notice: " in _plain_rich_text(block.text)
    assert "hello" in _plain_rich_text(block.text)


# ── Dashboard: capabilities + context, no hedges ─────────────────────


class _DashboardAdapter:
    def __init__(self):
        self._client = object()
        self.gateway_runner = None
        self.requests = []

    def _provider_ui(self):
        return None

    def _peer_for(self, chat_id):
        return ("peer", str(chat_id))

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=901)

    @staticmethod
    def _sent_message_id(response):
        return str(response.id)


def _dashboard_source():
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42",
                         chat_type="dm", profile="alpha", thread_id=None)


@pytest.mark.asyncio
async def test_dashboard_shows_capabilities_and_context_window():
    from plugins.platforms.telegram.model_settings_ui import TelegramModelSettingsUI

    adapter = _DashboardAdapter()
    ui = TelegramModelSettingsUI(adapter)
    dashboard = {"current_model": "vendor/model-a", "current_provider": "openrouter",
                 "provider_name": "OpenRouter", "provider_configured": True,
                 "model_catalog_count": 3, "reasoning_supported": True,
                 "session_settings": {}, "reasoning_effort": "provider default",
                 "capabilities": "reasoning, tools, vision", "context_window": "256k"}
    await ui.open_dashboard(_dashboard_source(), dashboard, [], "vendor/model-a",
                            "openrouter", "session-A", "/tmp/profile-a")
    state = next(iter(ui._states.values()))
    rich, _markup = ui._dashboard_view(state)
    text = _plain_rich_text(rich)
    assert "Capabilities: reasoning, tools, vision" not in text
    assert "Capabilities:" in text
    assert _dashboard_list_items(rich) == ["reasoning", "tools", "vision"]
    assert "Context window: 256k" in text
    assert "Catalog:" not in text
    assert "Support: provider reports support" in text
    assert "unconfirmed" not in text
    assert "Page 1 of 1" not in text
    assert "Changes apply to this conversation only." in text


@pytest.mark.asyncio
async def test_dashboard_omits_capabilities_when_unknown():
    from plugins.platforms.telegram.model_settings_ui import TelegramModelSettingsUI

    adapter = _DashboardAdapter()
    ui = TelegramModelSettingsUI(adapter)
    dashboard = {"current_model": "vendor/model-a", "current_provider": "openrouter",
                 "provider_name": "OpenRouter", "provider_configured": True,
                 "model_catalog_count": 3, "reasoning_supported": None,
                 "session_settings": {}, "reasoning_effort": "provider default",
                 "capabilities": "", "context_window": ""}
    await ui.open_dashboard(_dashboard_source(), dashboard, [], "vendor/model-a",
                            "openrouter", "session-A", "/tmp/profile-a")
    state = next(iter(ui._states.values()))
    rich, _markup = ui._dashboard_view(state)
    text = _plain_rich_text(rich)
    assert "Capabilities:" not in text
    assert "Context window:" not in text
    assert "Support: not reported" in text


def test_dashboard_payload_carries_capabilities_and_context(monkeypatch):
    from gateway.model_ui import dashboard_payload, render_dashboard

    monkeypatch.setattr(
        "agent.models_dev.get_model_info", lambda *args, **kwargs: _model_info())
    payload = dashboard_payload(
        current_model="vendor/model-a", current_provider="openrouter",
        providers=[{"slug": "openrouter", "name": "OpenRouter", "configured": True,
                    "total_models": 65, "reasoning_supported": True}],
        settings={}, reasoning_effort="",
    )
    assert payload["capabilities"] == "reasoning, tools, vision"
    assert payload["context_window"] == "256k"
    text = render_dashboard(payload)
    assert "Capabilities:\n• reasoning\n• tools\n• vision" in text
    assert "Context window: 256k" in text
    assert "Reasoning: supported" in text
    assert "Provider model catalog:" not in text
    assert "live entries" not in text
    assert "not confirmed" not in text


def test_dashboard_payload_is_empty_when_the_model_is_unknown(monkeypatch):
    from gateway.model_ui import dashboard_payload

    monkeypatch.setattr("agent.models_dev.get_model_info", lambda *args, **kwargs: None)
    payload = dashboard_payload(
        current_model="vendor/nope", current_provider="openrouter",
        providers=[{"slug": "openrouter", "name": "OpenRouter", "configured": True,
                    "total_models": 65, "reasoning_supported": None}],
        settings={}, reasoning_effort="",
    )
    assert payload["capabilities"] == ""
    assert payload["context_window"] == ""

# ── Dashboard cleanup: no hint/Catalog lines, capabilities as bullets,
#    counted Providers / List-change buttons ──────────────────────────


def _dashboard_list_items(rich):
    from telethon import types

    items = []
    for block in rich.blocks:
        if isinstance(block, types.PageBlockList):
            for item in block.items:
                if isinstance(item, types.PageListItemText):
                    items.append(_plain_rich_text(item.text))
    return items


@pytest.mark.asyncio
async def test_dashboard_view_drops_hint_and_catalog_and_bullets_capabilities():
    from plugins.platforms.telegram.model_settings_ui import TelegramModelSettingsUI

    adapter = _DashboardAdapter()
    ui = TelegramModelSettingsUI(adapter)
    dashboard = {"current_model": "vendor/model-a", "current_provider": "openrouter",
                 "provider_name": "OpenRouter", "provider_configured": True,
                 "model_catalog_count": 65, "configured_providers_count": 2,
                 "reasoning_supported": True,
                 "session_settings": {}, "reasoning_effort": "provider default",
                 "capabilities": "reasoning, tools, vision", "context_window": "256k"}
    await ui.open_dashboard(_dashboard_source(), dashboard, [], "vendor/model-a",
                            "openrouter", "session-A", "/tmp/profile-a")
    state = next(iter(ui._states.values()))
    rich, markup = ui._dashboard_view(state)
    text = _plain_rich_text(rich)
    assert "Providers opens the provider list" not in text
    assert "Catalog:" not in text
    assert "Capabilities: reasoning, tools, vision" not in text
    assert "Capabilities:" in text
    assert _dashboard_list_items(rich) == ["reasoning", "tools", "vision"]
    assert "Context window: 256k" in text
    assert "Support: provider reports support" in text
    assert "Changes apply to this conversation only." in text
    buttons = _keyboard_buttons(markup)
    assert ("Providers · 2", f"ms:{state['id']}:providers") in buttons
    assert ("List/change · 65", f"ms:{state['id']}:models") in buttons
    assert ("Model settings", f"ms:{state['id']}:settings") in buttons
    assert ("Close", f"ms:{state['id']}:close") in buttons


@pytest.mark.asyncio
async def test_dashboard_view_list_change_button_omits_count_when_uncatalogued():
    from plugins.platforms.telegram.model_settings_ui import TelegramModelSettingsUI

    adapter = _DashboardAdapter()
    ui = TelegramModelSettingsUI(adapter)
    dashboard = {"current_model": "vendor/model-a", "current_provider": "openrouter",
                 "provider_name": "OpenRouter", "provider_configured": True,
                 "model_catalog_count": 0, "configured_providers_count": 0,
                 "reasoning_supported": None,
                 "session_settings": {}, "reasoning_effort": "provider default",
                 "capabilities": "", "context_window": ""}
    await ui.open_dashboard(_dashboard_source(), dashboard, [], "vendor/model-a",
                            "openrouter", "session-A", "/tmp/profile-a")
    state = next(iter(ui._states.values()))
    _rich, markup = ui._dashboard_view(state)
    buttons = _keyboard_buttons(markup)
    assert ("List/change", f"ms:{state['id']}:models") in buttons
    assert not any(label.startswith("List/change ·") for label, _data in buttons)
    assert ("Providers · 0", f"ms:{state['id']}:providers") in buttons


def test_dashboard_payload_counts_configured_providers(monkeypatch):
    from gateway.model_ui import dashboard_payload

    monkeypatch.setattr(
        "agent.models_dev.get_model_info", lambda *args, **kwargs: _model_info())
    payload = dashboard_payload(
        current_model="vendor/model-a", current_provider="openrouter",
        providers=[{"slug": "openrouter", "name": "OpenRouter", "configured": True,
                    "total_models": 65, "reasoning_supported": True},
                   {"slug": "kilocode", "name": "Kilo Code", "configured": True,
                    "total_models": 12},
                   {"slug": "nous", "name": "Nous", "configured": False,
                    "total_models": 70}],
        settings={}, reasoning_effort="",
    )
    assert payload["configured_providers_count"] == 2
    assert payload["model_catalog_count"] == 65


def test_render_dashboard_drops_hint_and_catalog_and_bullets_capabilities(monkeypatch):
    from gateway.model_ui import dashboard_payload, render_dashboard

    monkeypatch.setattr(
        "agent.models_dev.get_model_info", lambda *args, **kwargs: _model_info())
    payload = dashboard_payload(
        current_model="vendor/model-a", current_provider="openrouter",
        providers=[{"slug": "openrouter", "name": "OpenRouter", "configured": True,
                    "total_models": 65, "reasoning_supported": True}],
        settings={}, reasoning_effort="",
    )
    text = render_dashboard(payload)
    assert "Providers: `/model providers`" not in text
    assert "Provider model catalog:" not in text
    assert "entries · reasoning:" not in text
    assert "Capabilities: reasoning, tools, vision" not in text
    assert "Capabilities:\n• reasoning\n• tools\n• vision" in text
    assert "Context window: 256k" in text
    assert "Reasoning: supported" in text
    assert "Session request settings: provider defaults" in text
    assert "Model settings: use `/msettings`" in text
