"""Invariant tests for the Worker B (tools/MCP) fixes in the MCP catalog UI.

Covers: real failure reasons instead of bare exception types (B1), Back to the
catalog keeping the page (B2), no symbol markers on rows (A1), the custom-server
button label/style (A3), bold highlights (A4), and the real-path enable/remove
re-render contract.
"""
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from gateway.config import Platform
from plugins.platforms.telegram.mcp_management_ui import TelegramMCPManagementUI


@dataclass(frozen=True)
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    platform: Platform = Platform.TELEGRAM
    message_id: str = "50"
    profile: str = "profile-a"


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
        self.gateway_runner = None
        self.settings_ui = SimpleNamespace(
            open_custom_mcp=AsyncMock(return_value=True),
            open_mcp=AsyncMock(return_value=True),
        )

    def _peer_for(self, chat_id):
        return chat_id

    @staticmethod
    def _sent_message_id(response):
        return str(getattr(response, "id", "")) or None

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=55)

    async def delete_message(self, chat_id, message_id):
        return True

    def _settings_ui(self):
        return self.settings_ui


class OwnerRunner:
    def _model_settings_owner_error(self, source, profile_home):
        return None


def _entry(name="demo", **updates):
    item = {
        "name": name,
        "description": "A safe example integration",
        "source_url": "https://example.com/source",
        "endpoint_url": "",
        "transport": "http",
        "auth_type": "none",
        "auth_provider": "",
        "env": [],
        "needs_install": False,
    }
    item.update(updates)
    return item


def _state(ui, home: Path, source=Source(), *, catalog=None, servers=None):
    home.mkdir(parents=True, exist_ok=True)
    if not (home / "config.yaml").exists():
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    state = ui._new_state(source, home)
    state.update(
        catalog=list(catalog or [_entry()]),
        profile_servers=dict(servers or {}),
        message_id="55",
    )
    return state


def _buttons(markup):
    return {
        button.text: getattr(button, "button", button)
        for row in markup.rows for button in row.buttons
    }


def _labels(markup):
    return [button.text for row in markup.rows for button in row.buttons]


@pytest.mark.asyncio
async def test_expired_detail_index_shows_real_reason_not_exception_type(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramMCPManagementUI(adapter)
    state = _state(ui, tmp_path / "profile")
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:detail:99")
    assert "This catalog page expired; reopen the list." in state["notice"]
    assert "ValueError" not in state["notice"]


@pytest.mark.asyncio
async def test_back_to_catalog_keeps_the_page_the_user_came_from(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramMCPManagementUI(adapter)
    state = _state(ui, tmp_path / "profile",
                   catalog=[_entry(f"s{index:02d}") for index in range(20)])
    state["page"] = 2
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:detail:0")
    assert state["view"] == "detail"
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:home")
    assert state["view"] == "catalog"
    assert state["page"] == 2
    text, _markup = ui._render(state)
    assert "Page 3 of" in text


@pytest.mark.asyncio
async def test_catalog_rows_have_no_symbol_markers_and_enabled_row_is_green(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramMCPManagementUI(adapter)
    state = _state(ui, tmp_path / "profile",
                   catalog=[_entry("alpha"), _entry("beta"), _entry("gamma")],
                   servers={"alpha": True, "beta": False})
    text, markup = ui._render(state)
    buttons = _buttons(markup)
    for label in buttons:
        assert "✓" not in label and "⏸" not in label and "＋" not in label
    assert buttons["alpha"].style.bg_success is True
    assert not buttons["beta"].style.bg_success
    assert "enabled" in text and "disabled" in text and "available" in text


def test_custom_server_button_names_the_action_and_is_not_green(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramMCPManagementUI(adapter)
    state = _state(ui, tmp_path / "profile")
    _text, markup = ui._render(state)
    buttons = _buttons(markup)
    assert "Add custom MCP" in buttons
    assert "Custom" not in buttons
    assert buttons["Add custom MCP"].style.bg_primary is True
    assert not buttons["Add custom MCP"].style.bg_success


def test_catalog_header_and_detail_highlight_the_values(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramMCPManagementUI(adapter)
    state = _state(ui, tmp_path / "profile",
                   catalog=[_entry("alpha", env=[{"name": "TOKEN", "prompt": "Token",
                                                  "required": True, "secret": True}])],
                   servers={"alpha": True})
    state["env_present"] = {"TOKEN": True}
    text, _markup = ui._render(state)
    assert "**1** configured · **1** enabled" in text
    assert "**Custom profile servers:** **0**" in text
    state.update(view="detail", selected_name="alpha")
    text, _markup = ui._render(state)
    assert "**Configured · enabled**" in text
    assert "**1/1** required value(s) present" in text


@pytest.mark.asyncio
async def test_install_failure_names_a_vanished_entry(tmp_path, monkeypatch):
    from hermes_cli import mcp_catalog

    adapter = FakeAdapter()
    adapter.gateway_runner = OwnerRunner()
    ui = TelegramMCPManagementUI(adapter)
    home = tmp_path / "profile"
    state = _state(ui, home, catalog=[_entry("demo")])
    state.update(view="detail", selected_name="demo")
    monkeypatch.setattr(mcp_catalog, "get_entry", lambda _name: None)
    await ui._perform_install(state, "demo", {})
    assert "Catalog entry no longer exists" in state["notice"]
    assert "ValueError" not in state["notice"]


@pytest.mark.asyncio
async def test_enable_toggle_and_remove_re_render_the_detail_with_new_state(tmp_path):
    from hermes_cli import mcp_catalog

    real = mcp_catalog.list_catalog()[0].name
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "mcp_servers": {real: {"url": "https://example.invalid/mcp", "enabled": False}},
    }), encoding="utf-8")
    adapter = FakeAdapter()
    adapter.gateway_runner = OwnerRunner()
    ui = TelegramMCPManagementUI(adapter)
    state = ui._new_state(Source(), home)
    with ui._profile_scope(state):
        ui._refresh_snapshot(state)
    state["message_id"] = "55"
    index = next(i for i, item in enumerate(ui._pages(state)) if item["name"] == real)
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:detail:{index}")
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:enable-toggle")
    assert f"{real} enabled in this profile." in state["notice"]
    text, markup = ui._render(state)
    assert "Disable" in _labels(markup)
    assert "Configured · enabled" in text
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:home")
    _text, markup = ui._render(state)
    assert _buttons(markup)[real].style.bg_success is True
    index = next(i for i, item in enumerate(ui._pages(state)) if item["name"] == real)
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:detail:{index}")
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:remove-ask")
    assert state["view"] == "confirm-remove"
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:remove")
    assert "Removed from profile." in state["notice"]
    text, markup = ui._render(state)
    assert "Add to profile…" in _labels(markup)
    assert "Not configured" in text
