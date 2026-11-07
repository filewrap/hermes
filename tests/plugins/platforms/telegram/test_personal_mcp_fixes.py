"""Invariant tests for the Worker B (tools/MCP) fixes in the personal MCP UI.

Covers: custom-MCP save failures showing the specific reason on the MCP list
(B4), the selection toggle flipping its green state (Bug 2 pin), no check-mark
in toggle labels (A2), and the /setup "Personal MCP controls" deep link (B6).
"""
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@dataclass(frozen=True)
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    profile: str | None = None


class CallbackEvent:
    def __init__(self, message_id):
        self.message_id = message_id
        self.answers = []

    async def answer(self, text=None):
        self.answers.append(text or "")


class FakeAdapter:
    def __init__(self):
        self._client = object()
        self.edits = []
        self._next_message_id = 100

    def _peer_for(self, chat_id):
        return chat_id

    async def _mtproto_rich_request(self, request):
        self.edits.append(request)
        self._next_message_id += 1
        return SimpleNamespace(id=self._next_message_id)

    @staticmethod
    def _sent_message_id(response):
        return str(response.id)


def _bind_profile_home(adapter, home):
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: home,
    )


def _buttons_by_text(markup):
    return {
        button.text: button
        for row in markup.rows for wrapped in row.buttons
        for button in [getattr(wrapped, "button", wrapped)]
    }


def _profile_with_server(home, name="profile-search"):
    (home / "config.yaml").write_text(
        "mcp_servers:\n"
        f"  {name}:\n"
        "    url: https://profile.example.test/mcp\n"
        "    enabled: true\n",
        encoding="utf-8",
    )


def _new_state(ui, home, source=Source()):
    state = ui._new_state(source, home, message_id=55)
    state["user_name"] = source.user_name
    return state


@pytest.mark.asyncio
async def test_mcp_add_confirm_duplicate_name_shows_reason_on_mcp_list(tmp_path):
    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    _profile_with_server(tmp_path)
    state = _new_state(ui, tmp_path)
    state["pending_mcp"] = {"name": "profile-search", "config": {"url": "https://example.test/mcp"}}
    captured = {}

    async def _capture(_state, text, markup):
        captured["text"] = text
        captured["markup"] = markup

    ui._edit = _capture
    assert await ui.handle_callback(CallbackEvent(55), Source(), f"ps:{state['id']}:mcp-add-confirm")
    assert "# Personal MCP servers" in captured["text"]
    assert "already exists in profile or personal settings" in captured["text"]
    assert "failed safely" not in captured["text"]


@pytest.mark.asyncio
async def test_mcp_add_confirm_catalog_reserved_name_shows_reason(tmp_path):
    from hermes_cli import mcp_catalog

    reserved = mcp_catalog.list_catalog()[0].name
    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    state = _new_state(ui, tmp_path)
    state["pending_mcp"] = {"name": reserved, "config": {"url": "https://example.test/mcp"}}
    captured = {}

    async def _capture(_state, text, markup):
        captured["text"] = text
        captured["markup"] = markup

    ui._edit = _capture
    assert await ui.handle_callback(CallbackEvent(55), Source(), f"ps:{state['id']}:mcp-add-confirm")
    assert "# Personal MCP servers" in captured["text"]
    assert "reserved by the curated catalog" in captured["text"]


@pytest.mark.asyncio
async def test_mcp_toggle_flips_green_state_without_check_marks(tmp_path):
    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    _profile_with_server(tmp_path)
    state = _new_state(ui, tmp_path)
    captured = {}

    async def _capture(_state, text, markup):
        captured["text"] = text
        captured["markup"] = markup

    ui._edit = _capture
    await ui._show_mcp(state)
    buttons = _buttons_by_text(captured["markup"])
    assert "profile-search" in buttons
    assert not any("✓" in label for label in buttons)
    assert not buttons["profile-search"].style.bg_success
    assert await ui.handle_callback(CallbackEvent(55), Source(), f"ps:{state['id']}:mcp-toggle:0")
    buttons = _buttons_by_text(captured["markup"])
    assert buttons["profile-search"].style.bg_success is True
    assert "| **profile-search** | Selected |" in captured["text"]
    assert await ui.handle_callback(CallbackEvent(55), Source(), f"ps:{state['id']}:mcp-toggle:0")
    buttons = _buttons_by_text(captured["markup"])
    assert not buttons["profile-search"].style.bg_success
    assert "| **profile-search** | Not selected |" in captured["text"]


def test_mcp_servers_view_has_no_check_marks():
    from plugins.platforms.telegram.rich_ui import mcp_servers_view

    text, markup = mcp_servers_view([("search", True), ("other", False)], 7)
    labels = [button.text for row in markup.rows for button in row.buttons]
    assert not any("✓" in label for label in labels)
    assert "| **search** | Selected |" in text
    assert "| **other** | Not selected |" in text


@pytest.mark.asyncio
async def test_control_dispatch_mcp_opens_the_mcp_view_not_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda _home: False)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm", user_id="100")
    adapter = TelethonAdapter(PlatformConfig())
    adapter._authorized = lambda _source: True
    adapter._is_owner_user = lambda uid, source=None: uid == "100"
    adapter._send_rich_ui = AsyncMock()
    settings_ui = SimpleNamespace(open_mcp=AsyncMock(return_value=True))
    adapter._settings_ui = lambda: settings_ui
    await adapter._dispatch_control_command(source, "mcp", reply_to=55)
    settings_ui.open_mcp.assert_awaited_once_with(source, reply_to=55)
    adapter._send_rich_ui.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_inventory_mcp_quick_link_goes_to_the_mcp_view(tmp_path):
    from plugins.platforms.telegram.setup_cli_inventory import command_inventory
    from plugins.platforms.telegram.setup_ui import TelegramSetupUI

    class _SetupAdapter:
        def __init__(self, home):
            self.home = home
            self._client = object()
            self.gateway_runner = SimpleNamespace(
                _resolve_profile_home_for_source=lambda _source: home,
            )

        def _authorized(self, source):
            return True

        def _is_owner_user(self, user_id, *, source=None):
            return str(user_id) == "101"

    home = tmp_path / "profile"
    adapter = _SetupAdapter(home)
    ui = TelegramSetupUI(adapter)
    source = SimpleNamespace(chat_type="dm", user_id="101", chat_id="101",
                             profile="alpha", thread_id=None)
    state = ui._new_state(source, home, "inventory")
    node_id = next(index for index, node in enumerate(command_inventory())
                   if node["path"] == ["mcp"])
    state["inventory_node"] = node_id
    state["page"] = 1
    _text, markup = ui._inventory_view(state)
    payloads = [bytes(getattr(wrapped, "button", wrapped).type.data).decode()
                for row in markup.rows for wrapped in row.buttons]
    labels = [getattr(wrapped, "button", wrapped).text
              for row in markup.rows for wrapped in row.buttons]
    assert "Personal MCP controls" in labels
    index = labels.index("Personal MCP controls")
    assert payloads[index].endswith(":nav:mcp")
