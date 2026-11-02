from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from telethon import types

from plugins.platforms.telegram.mtproto_rich import edit_request, send_request
from plugins.platforms.telegram.plugin_management_ui import TelegramPluginManagementUI
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class Source:
    chat_id = "123"
    user_id = "123"
    thread_id = None
    chat_type = "dm"
    profile = "profile-a"
    is_bot = False


class CallbackEvent:
    def __init__(self, message_id):
        self.message_id = message_id
        self.answers = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text or "", alert))


class FakeAdapter:
    def __init__(self, profile_home):
        self.profile_home = Path(profile_home)
        self.gateway_runner = SimpleNamespace(
            _resolve_profile_home_for_source=lambda _source: self.profile_home,
        )
        self.requests = []
        self.edits = []
        self._next_id = 50

    def _authorized(self, _source):
        return True

    def _is_owner_user(self, user_id, _source=None):
        return str(user_id) == "123"

    def _peer_for(self, _chat_id):
        return types.PeerUser(123)

    @staticmethod
    def _sent_message_id(result):
        return str(result.id)

    async def _send_rich_ui(self, source, text, markup=None, reply_to=None):
        self._next_id += 1
        request = send_request(self._peer_for(source.chat_id), text, reply_to=reply_to,
                               top_msg_id=None, reply_markup=markup)
        self.requests.append(request)
        return SimpleNamespace(id=self._next_id)

    async def _edit_rich_ui_message(self, event, source, text, markup=None):
        request = edit_request(self._peer_for(source.chat_id), event.message_id, text,
                               reply_markup=markup)
        self.edits.append(request)
        return SimpleNamespace(id=event.message_id)


def _install_plugin(home: Path, *, capabilities=None):
    plugin_dir = home / "plugins" / "sample-plugin"
    plugin_dir.mkdir(parents=True)
    manifest = {
        "name": "sample-plugin",
        "version": "1.2.3",
        "description": "A test plugin for the native plugin manager.",
    }
    if capabilities is not None:
        manifest["capabilities"] = capabilities
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (home / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
    return plugin_dir


def _plugin_row(state):
    return next(row for row in state["plugins"] if row["key"] == "sample-plugin")


def _walk_rich(nodes):
    stack = list(nodes) if isinstance(nodes, (list, tuple)) else [nodes]
    while stack:
        node = stack.pop()
        if isinstance(node, types.TLObject):
            yield node
            children = []
            for value in vars(node).values():
                if isinstance(value, (list, tuple)):
                    children.extend(value)
                elif isinstance(value, types.TLObject):
                    children.append(value)
            stack.extend(reversed(children))


def _rich_text(message):
    if hasattr(message, "rich_message"):
        message = message.rich_message
    markdown = getattr(message, "markdown", None)
    if markdown is not None:
        return markdown
    return _plain_rich_text(message)


def _plain_rich_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_plain_rich_text(child) for child in value)
    if isinstance(value, types.TextPlain):
        return value.text
    if isinstance(value, types.TextConcat):
        return "".join(_plain_rich_text(child) for child in value.texts)
    if isinstance(value, types.TextButton):
        return _plain_rich_text(value.text)
    if isinstance(value, types.InputRichMessage):
        return "\n".join(_plain_rich_text(block) for block in value.blocks)
    if isinstance(value, types.PageBlockTable):
        return "\n".join(" | ".join(_plain_rich_text(cell.text) for cell in row.cells)
                         for row in value.rows)
    if isinstance(value, types.PageBlockList):
        return "\n".join(_plain_rich_text(item.text) for item in value.items)
    if hasattr(value, "texts"):
        return "".join(_plain_rich_text(child) for child in value.texts)
    if hasattr(value, "text"):
        return _plain_rich_text(value.text)
    if hasattr(value, "blocks"):
        return "\n".join(_plain_rich_text(block) for block in value.blocks)
    return ""


def _button_color(button):
    style = button.style
    return [name for name in ("bg_success", "bg_danger") if getattr(style, name, False)]


def _callback_data(button):
    assert isinstance(button.type, types.InlineButtonTypeCallback)
    return button.type.data.decode("ascii")


async def _click(ui, adapter, source, state, action, value=""):
    data = f"pl:{state['id']}:{action}" + (f":{value}" if value else "")
    return await ui.handle_callback(CallbackEvent(state["message_id"]), source, data)


def _config(ui, state):
    from hermes_cli.config import load_config
    with ui._profile_scope(state):
        return load_config()


@pytest.mark.asyncio
async def test_open_sends_native_rich_message_with_interactive_inline_buttons(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    plugin_dir = _install_plugin(home)
    source = Source()
    adapter = FakeAdapter(home)
    ui = TelegramPluginManagementUI(adapter)

    from hermes_cli import plugins_cmd
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: [
        ("sample-plugin", "1.2.3", "A test plugin for the native plugin manager.",
         "user", plugin_dir, "sample-plugin"),
    ])

    assert await ui.open(source, profile_home=home, reply_to=7)
    request = adapter.requests[-1]
    assert isinstance(request.rich_message, types.InputRichMessage)
    assert bytes(request.rich_message)
    assert isinstance(request.reply_markup, types.ReplyInlineMarkup)
    body = request.rich_message
    assert isinstance(body.blocks[0], types.PageBlockHeading1)
    assert body.blocks[0].text.text == "General plugins"
    assert "Installed plugins for this profile." in _rich_text(body)
    assert "sample-plugin" in _rich_text(body) and "Not enabled" in _rich_text(body)
    assert "Page 1 of 1 • total 1" in _rich_text(body)
    assert "Actions" not in _rich_text(body)
    assert any(isinstance(node, types.PageBlockTable) for node in _walk_rich(body.blocks))
    rich_buttons = [node for node in _walk_rich(body.blocks) if isinstance(node, types.TextButton)]
    assert {button.text.text for button in rich_buttons} == {
        "1 installed", "0 enabled", "0 disabled", "1 not enabled", "Not enabled",
    }
    by_label = {button.text.text: button for button in rich_buttons}
    assert _button_color(by_label["0 enabled"]) == ["bg_success"]
    assert _button_color(by_label["0 disabled"]) == ["bg_danger"]
    assert _button_color(by_label["Not enabled"]) == ["bg_danger"]
    assert isinstance(body.blocks[-1].text, types.TextItalic)
    assert [button.text for row in request.reply_markup.rows for button in row.buttons]
    assert ui._states


@pytest.mark.asyncio
async def test_list_pages_have_ten_name_buttons_two_columns_and_native_status_table(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    plugin_dir = _install_plugin(home)
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - plugin-10\n", encoding="utf-8")
    source = Source()
    adapter = FakeAdapter(home)
    ui = TelegramPluginManagementUI(adapter)
    from hermes_cli import plugins_cmd

    plugins = [(f"plugin-{index:02d}", "1.0", f"Description {index}", "user",
                plugin_dir, f"plugin-{index:02d}") for index in range(21)]
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: plugins)

    assert await ui.open(source, profile_home=home)
    state = next(iter(ui._states.values()))
    await _click(ui, adapter, source, state, "page", "1")

    request = adapter.edits[-1]
    assert isinstance(request.rich_message, types.InputRichMessage)
    body = _rich_text(request.rich_message)
    assert "plugin-11" in body and "plugin-19" in body
    assert "plugin-20" not in body
    assert "Actions" not in body
    table = next(node for node in _walk_rich(request.rich_message.blocks)
                 if isinstance(node, types.PageBlockTable))
    assert len(table.rows) == 11
    enabled_status = next(node for node in _walk_rich(table.rows)
                          if isinstance(node, types.TextButton) and node.text.text == "enabled")
    assert _button_color(enabled_status) == ["bg_success"]

    markup = request.reply_markup
    assert isinstance(markup, types.ReplyInlineMarkup)
    actions = [[_callback_data(button).split(":", 3)[2]
                for button in row.buttons if isinstance(button.type, types.InlineButtonTypeCallback)]
               for row in markup.rows]
    plugin_rows = [row for row in markup.rows if any(
        _callback_data(button).split(":", 3)[2] == "detail" for button in row.buttons
    )]
    assert [len(row.buttons) for row in plugin_rows] == [2, 2, 2, 2, 2]
    assert len({button.text for row in plugin_rows for button in row.buttons}) == 10
    control_actions = [action for row in actions for action in row if action != "detail"]
    assert control_actions.count("filter") == 4
    assert control_actions.count("page") == 2
    assert control_actions.count("refresh") == 1
    assert control_actions.count("close") == 1

    assert "Page 2 of 3 • total 21" in body


@pytest.mark.asyncio
async def test_native_rich_body_and_selector_keyboard_share_one_mtproto_request():
    from telethon import functions

    status = types.TextButton(
        text=types.TextPlain(text="Enabled"),
        type=types.InlineButtonTypeCallback(data=b"pl:1:status"),
        style=types.RichButtonStyle(bg_success=True),
    )
    rich = types.InputRichMessage(blocks=[types.PageBlockParagraph(text=status)])
    markup = types.ReplyInlineMarkup(rows=[types.KeyboardButtonRow(buttons=[
        types.KeyboardInlineButton(
            text="sample-plugin", type=types.InlineButtonTypeCallback(data=b"pl:1:detail:0"),
        ),
    ])])
    adapter = SimpleNamespace(
        _client=object(),
        _peer_for=Mock(return_value=types.InputPeerUser(user_id=123, access_hash=0)),
        _mtproto_rich_request=AsyncMock(return_value=SimpleNamespace(id=89)),
    )
    source = SimpleNamespace(chat_id="123", thread_id="77")

    result = await TelethonAdapter._send_rich_ui(adapter, source, rich, markup, reply_to=7)

    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.rich_message is rich
    assert request.reply_markup is markup
    assert request.reply_to.reply_to_msg_id == 7
    assert request.reply_to.top_msg_id == 77
    assert bytes(request)
    assert result.id == 89


@pytest.mark.asyncio
async def test_owner_bound_detail_and_enable_toggle_updates_profile_config(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    plugin_dir = _install_plugin(home)
    source = Source()
    adapter = FakeAdapter(home)
    ui = TelegramPluginManagementUI(adapter)
    from hermes_cli import plugins_cmd
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: [
        ("sample-plugin", "1.2.3", "A test plugin for the native plugin manager.",
         "user", plugin_dir, "sample-plugin"),
    ])

    assert await ui.open(source, profile_home=home)
    state = next(iter(ui._states.values()))
    index = ui._filtered(state).index(_plugin_row(state))
    await _click(ui, adapter, source, state, "detail", str(index))
    assert state["view"] == "detail"
    detail = adapter.edits[-1].rich_message
    assert isinstance(detail, types.InputRichMessage)
    assert "sample-plugin" in _rich_text(detail)
    assert "Status: DISABLED" in _rich_text(detail)
    assert not any("Capabilities" in _rich_text(block) for block in detail.blocks)
    assert "Setup and behavior" not in _rich_text(detail)
    status_button = next(node for node in _walk_rich(detail.blocks)
                         if isinstance(node, types.TextButton))
    assert status_button.text.text == "DISABLED"
    assert _button_color(status_button) == ["bg_danger"]
    detail_edits = len(adapter.edits)
    await _click(ui, adapter, source, state, "status")
    assert len(adapter.edits) == detail_edits
    assert _config(ui, state)["plugins"].get("enabled", []) == []

    await _click(ui, adapter, source, state, "enable")
    config = _config(ui, state)
    assert config["plugins"]["enabled"] == ["sample-plugin"]
    assert config["plugins"]["disabled"] == []

    await _click(ui, adapter, source, state, "disable")
    config = _config(ui, state)
    assert "sample-plugin" not in config["plugins"]["enabled"]
    assert config["plugins"]["disabled"] == ["sample-plugin"]
    assert "No gateway restart was triggered" in _rich_text(adapter.edits[-1].rich_message)


@pytest.mark.asyncio
async def test_detail_description_is_not_truncated_and_enabled_status_is_green(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    plugin_dir = _install_plugin(home)
    long_description = "Full plugin description. " + ("A useful detail. " * 30)
    source = Source()
    adapter = FakeAdapter(home)
    ui = TelegramPluginManagementUI(adapter)
    from hermes_cli import plugins_cmd
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: [
        ("sample-plugin", "1.2.3", long_description, "user", plugin_dir, "sample-plugin"),
    ])
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - sample-plugin\n", encoding="utf-8")

    assert await ui.open(source, profile_home=home)
    state = next(iter(ui._states.values()))
    await _click(ui, adapter, source, state, "detail", "0")
    detail = adapter.edits[-1].rich_message
    assert detail.blocks[0].text.text == "sample-plugin"
    assert detail.blocks[1].text.text == long_description.strip()
    assert len(detail.blocks[1].text.text) > 320
    status_button = next(node for node in _walk_rich(detail.blocks)
                         if isinstance(node, types.TextButton))
    assert status_button.text.text == "ENABLED"
    assert _button_color(status_button) == ["bg_success"]
    assert "Capabilities" not in _rich_text(detail)
    assert "Setup and behavior" not in _rich_text(detail)


@pytest.mark.asyncio
async def test_capability_enable_requires_explicit_grant_or_no_grant_choice(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    plugin_dir = _install_plugin(home, capabilities=["gateway.platform_actions"])
    source = Source()
    adapter = FakeAdapter(home)
    ui = TelegramPluginManagementUI(adapter)
    from hermes_cli import plugins_cmd
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: [
        ("sample-plugin", "1.2.3", "A test plugin for the native plugin manager.",
         "user", plugin_dir, "sample-plugin"),
    ])

    assert await ui.open(source, profile_home=home)
    state = next(iter(ui._states.values()))
    index = ui._filtered(state).index(_plugin_row(state))
    await _click(ui, adapter, source, state, "detail", str(index))
    await _click(ui, adapter, source, state, "enable")
    assert state["view"] == "confirm-enable"
    confirmation = adapter.edits[-1].rich_message
    assert "not sandboxed" in _rich_text(confirmation)
    assert "Not granted" in _rich_text(confirmation)
    assert not any(isinstance(node, types.TextButton) for node in _walk_rich(_rich_text(confirmation)))
    assert any(button.text == "Enable without grants"
               for row in adapter.edits[-1].reply_markup.rows for button in row.buttons)
    assert "sample-plugin" not in (_config(ui, state)["plugins"].get("enabled") or [])

    await _click(ui, adapter, source, state, "enable-no-grants")
    config = _config(ui, state)
    assert config["plugins"]["enabled"] == ["sample-plugin"]
    assert "gateway.platform_actions" not in (
        config.get("plugins", {}).get("entries", {}).get("sample-plugin", {}).get("granted_capabilities", [])
    )


@pytest.mark.asyncio
async def test_grant_is_recorded_only_after_confirming_the_capability_screen(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    plugin_dir = _install_plugin(home, capabilities=["gateway.platform_actions"])
    source = Source()
    adapter = FakeAdapter(home)
    ui = TelegramPluginManagementUI(adapter)
    from hermes_cli import plugins_cmd
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: [
        ("sample-plugin", "1.2.3", "A test plugin for the native plugin manager.",
         "user", plugin_dir, "sample-plugin"),
    ])

    assert await ui.open(source, profile_home=home)
    state = next(iter(ui._states.values()))
    index = ui._filtered(state).index(_plugin_row(state))
    await _click(ui, adapter, source, state, "detail", str(index))
    await _click(ui, adapter, source, state, "enable")
    assert not (_config(ui, state).get("plugins", {}).get("entries", {}).get(
        "sample-plugin", {}).get("granted_capabilities"))

    await _click(ui, adapter, source, state, "grant")
    config = _config(ui, state)
    entry = config["plugins"]["entries"]["sample-plugin"]
    assert entry["granted_capabilities"] == ["gateway.platform_actions"]
    assert entry["allow_platform_actions"] is True
    assert config["plugins"]["enabled"] == ["sample-plugin"]


@pytest.mark.asyncio
async def test_callback_rejects_another_actor_or_another_card_without_mutating(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    plugin_dir = _install_plugin(home)
    source = Source()
    adapter = FakeAdapter(home)
    ui = TelegramPluginManagementUI(adapter)
    from hermes_cli import plugins_cmd
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: [
        ("sample-plugin", "1.2.3", "A test plugin for the native plugin manager.",
         "user", plugin_dir, "sample-plugin"),
    ])
    assert await ui.open(source, profile_home=home)
    state = next(iter(ui._states.values()))

    intruder = SimpleNamespace(chat_id="999", user_id="999", thread_id=None,
                               chat_type="dm", profile="profile-a", is_bot=False)
    denied = CallbackEvent(state["message_id"])
    await ui.handle_callback(denied, intruder, f"pl:{state['id']}:disable")
    assert denied.answers[-1][1] is True
    assert _config(ui, state)["plugins"].get("disabled", []) == []

    wrong_card = CallbackEvent(str(int(state["message_id"]) + 1))
    await ui.handle_callback(wrong_card, source, f"pl:{state['id']}:disable")
    assert wrong_card.answers[-1][1] is True
    assert _config(ui, state)["plugins"].get("disabled", []) == []


@pytest.mark.asyncio
async def test_admitted_non_owner_can_browse_but_cannot_mutate_shared_plugins(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    plugin_dir = _install_plugin(home)
    source = SimpleNamespace(chat_id="-1001", user_id="456", thread_id=None,
                             chat_type="group", profile="profile-a", is_bot=False)
    adapter = FakeAdapter(home)
    ui = TelegramPluginManagementUI(adapter)
    from hermes_cli import plugins_cmd
    monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: [
        ("sample-plugin", "1.2.3", "A test plugin for the native plugin manager.",
         "user", plugin_dir, "sample-plugin"),
    ])

    assert await ui.open(source, profile_home=home)
    state = next(iter(ui._states.values()))
    index = ui._filtered(state).index(_plugin_row(state))
    await _click(ui, adapter, source, state, "detail", str(index))
    body = _rich_text(adapter.edits[-1].rich_message)
    assert "sample-plugin" in body
    assert "configured owner in a private chat" in body
    assert not any(button.text in {"Enable", "Disable"}
                   for row in adapter.edits[-1].reply_markup.rows for button in row.buttons)

    denied = CallbackEvent(state["message_id"])
    await ui.handle_callback(denied, source, f"pl:{state['id']}:disable")
    assert denied.answers[-1][1] is True
    assert _config(ui, state)["plugins"].get("disabled", []) == []
