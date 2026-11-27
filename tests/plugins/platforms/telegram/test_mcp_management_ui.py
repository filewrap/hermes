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
        self.deleted = []
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
        self.deleted.append((str(chat_id), str(message_id)))
        return True

    def _settings_ui(self):
        return self.settings_ui


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


def _button_rows(markup):
    return [[(button.text, button.type.data.decode("utf-8")) for button in row.buttons]
            for row in markup.rows]


def _walk(node):
    from telethon import types
    yield node
    if isinstance(node, (list, tuple)):
        for child in node:
            yield from _walk(child)
    elif isinstance(node, types.TLObject):
        for child in vars(node).values():
            if isinstance(child, (types.TLObject, list, tuple)):
                yield from _walk(child)


def test_shipped_catalog_coverage_configured_first_and_custom_is_own_keyboard_row(tmp_path):
    from hermes_cli import mcp_catalog

    entries = mcp_catalog.list_catalog()
    assert len(entries) == 66
    configured_name = entries[-1].name
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "mcp_servers": {configured_name: {"url": "https://example.invalid/mcp", "enabled": True}},
    }), encoding="utf-8")
    source = Source()

    ui = TelegramMCPManagementUI(FakeAdapter())
    state = _state(ui, home, source)
    with ui._profile_scope(state):
        ui._refresh_snapshot(state)
    text, markup = ui._render(state)

    assert len(state["catalog"]) == 66
    assert state["catalog"][0]["name"] == configured_name
    assert "Browse 66 curated MCP integrations" in text
    assert "not a list of connected services" in text
    rows = _button_rows(markup)
    custom_row = next(row for row in rows if row and row[0][0] == "Add custom MCP")
    assert len(custom_row) == 1
    assert custom_row[0][1].endswith(":custom")
    assert all("Custom" not in label for row in rows if row != custom_row for label, _ in row)
    assert any(label == "My MCP settings" for row in rows for label, _ in row)


def test_catalog_profile_isolation_survives_a_b_a_reads(tmp_path):
    from hermes_cli import mcp_catalog
    first = mcp_catalog.list_catalog()[0].name
    second = mcp_catalog.list_catalog()[1].name
    homes = {"profile-a": tmp_path / "a", "profile-b": tmp_path / "b"}
    selections = {"profile-a": first, "profile-b": second}
    for profile, home in homes.items():
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({
            "mcp_servers": {selections[profile]: {"url": f"https://{profile}.example.invalid/mcp", "enabled": True}},
        }), encoding="utf-8")

    ui = TelegramMCPManagementUI(FakeAdapter())
    observed = []
    for profile in ("profile-a", "profile-b", "profile-a"):
        source = Source(profile=profile)
        state = ui._new_state(source, homes[profile])
        with ui._profile_scope(state):
            ui._refresh_snapshot(state)
        observed.append((set(state["profile_servers"]), state["profile_servers"][selections[profile]]))

    assert observed[0] == ({first}, True)
    assert observed[1] == ({second}, True)
    assert observed[2] == ({first}, True)


def test_detail_serializes_native_rich_url_copy_control_and_regular_keyboard(tmp_path):
    from telethon import types
    from plugins.platforms.telegram.mtproto_rich import send_request

    adapter = FakeAdapter()
    ui = TelegramMCPManagementUI(adapter)
    state = _state(ui, tmp_path / "profile", catalog=[_entry()])
    state.update(view="detail", selected_name="demo")
    text, markup = ui._render(state)
    request = send_request(types.PeerUser(123), text, reply_markup=markup)
    nodes = list(_walk(request.rich_message))

    assert any(isinstance(node, types.TextUrl) and node.url == "https://example.com/source" for node in nodes)
    assert any(isinstance(node, types.TextButton)
               and isinstance(node.type, types.InlineButtonTypeCopy)
               and node.type.copy_text == "hermes mcp install demo" for node in nodes)
    assert isinstance(request.reply_markup, types.ReplyInlineMarkup)
    assert len(request._bytes()) > 0
    labels = [label for row in _button_rows(markup) for label, _ in row]
    assert "Add to profile…" in labels and "Back to catalog" in labels


@pytest.mark.asyncio
async def test_custom_button_hands_same_card_to_existing_private_custom_flow(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramMCPManagementUI(adapter)
    state = _state(ui, tmp_path / "profile")
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:custom")
    adapter.settings_ui.open_custom_mcp.assert_awaited_once_with(Source(), message_id="55")
    assert not ui._states


@pytest.mark.asyncio
async def test_private_selection_button_hands_off_without_owner_gate(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramMCPManagementUI(adapter)
    state = _state(ui, tmp_path / "profile")
    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:my-selection")
    adapter.settings_ui.open_mcp.assert_awaited_once_with(Source(), message_id="55")
    assert not ui._states


@pytest.mark.asyncio
async def test_shared_install_is_owner_gated_and_requires_confirmation(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    ui = TelegramMCPManagementUI(adapter)
    home = tmp_path / "profile"
    state = _state(ui, home)
    state.update(view="detail", selected_name="demo")

    class Runner:
        def _model_settings_owner_error(self, source, profile_home):
            assert Path(profile_home) == home
            return None if source.user_id == "123" else "Only owner."

    adapter.gateway_runner = Runner()
    perform = AsyncMock()
    monkeypatch.setattr(ui, "_begin_install", perform)

    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:install-ask")
    assert state["view"] == "confirm-install"
    perform.assert_not_awaited()

    await ui.handle_callback(CallbackEvent(55), Source(), f"mc:{state['id']}:install")
    perform.assert_awaited_once_with(state)
