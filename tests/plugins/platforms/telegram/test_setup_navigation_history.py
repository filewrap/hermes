"""Behavioral regressions for Telegram setup navigation on a shared control card."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions

from gateway.config import PlatformConfig
from plugins.platforms.telegram.setup_fields import HOME_PAGE_SIZE
from plugins.platforms.telegram.setup_ui import TelegramSetupUI
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


@pytest.fixture
def real_control(tmp_path):
    adapter = TelethonAdapter(PlatformConfig())
    homes = {name: tmp_path / name for name in ("alpha", "beta")}
    for home in homes.values():
        home.mkdir()
    source = SimpleNamespace(
        chat_type="dm", chat_id="101", user_id="101", user_name="Owner",
        profile="alpha", thread_id=None, is_bot=False,
    )
    adapter._settings_home = lambda update: homes[update.profile]
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda update: homes[update.profile])
    adapter._peer_for = lambda _chat_id: "peer-101"
    adapter._authorized = lambda update: update.chat_type == "dm"
    adapter._is_owner_user = lambda user_id, source=None: str(user_id) == "101"
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=501))
    adapter._control_route_store = {}
    setup = adapter._telegram_setup_ui = TelegramSetupUI(adapter)
    return adapter, setup, source, homes


def _payload(markup, label: str) -> str:
    for row in markup.rows:
        for button in row.buttons:
            if button.text == label:
                data = button.type.data
                return data.decode() if isinstance(data, bytes) else data
    raise AssertionError(f"button {label!r} not found")


def _action_payload(markup, action: str) -> str:
    for row in markup.rows:
        for button in row.buttons:
            data = button.type.data
            data = data.decode() if isinstance(data, bytes) else data
            if data.endswith(action):
                return data
    raise AssertionError(f"callback action {action!r} not found")


def _settings_action_payload(request, action: str) -> str:
    """Read a settings payload from the keyboard, or from an in-body tg-button."""
    try:
        return _action_payload(request.reply_markup, action)
    except (AssertionError, AttributeError):
        pass
    import re
    html = getattr(request, "message", "") or ""
    match = re.search(r'data="([^"]*' + re.escape(action) + r')"', html)
    if match:
        return match.group(1)
    raise AssertionError(f"callback action {action!r} not found")


def _last_request(adapter):
    return adapter._client.await_args.args[0]


def _callback(message_id: int = 501):
    return SimpleNamespace(message_id=message_id, answer=AsyncMock())


async def _click(adapter, source, markup, label: str, *, callback=None):
    callback = callback or _callback()
    assert await adapter._handle_control_callback(callback, source, _payload(markup, label))
    return callback, _last_request(adapter)


async def _open_home_page(adapter, setup, source, page: int):
    await setup.handle_command(source)
    for target in range(2, page + 1):
        state = next(iter(setup._states.values()))
        _callback_event, request = await _click(
            adapter, source,
            _last_request(adapter).reply_markup,
            f"Next · {target}/{setup._page_count('home')}",
        )
        assert isinstance(request, functions.messages.EditMessageRequest)
        state = next(iter(setup._states.values()))
        assert state["page"] == target
    return next(iter(setup._states.values()))


@pytest.mark.parametrize(
    ("button", "heading"),
    [("Browser", "Browser service"), ("Dashboard / Web UI", "Dashboard / WebUI")],
)
@pytest.mark.asyncio
async def test_home_page_three_delegated_surface_back_restores_exact_page(
    real_control, button, heading,
):
    adapter, setup, source, _homes = real_control
    state = await _open_home_page(adapter, setup, source, 3)
    assert state["page"] == 3
    action = "nav:browser" if button == "Browser" else "nav:dashboard"
    callback = _callback()
    assert await adapter._handle_control_callback(
        callback, source, _action_payload(_last_request(adapter).reply_markup, action),
    )

    delegated = _last_request(adapter)
    assert isinstance(delegated, functions.messages.EditMessageRequest)
    assert delegated.id == 501
    assert heading in delegated.message
    back = _payload(delegated.reply_markup, "← Back to setup")
    callback = _callback()
    assert await adapter._handle_control_callback(callback, source, back)

    home = _last_request(adapter)
    assert isinstance(home, functions.messages.EditMessageRequest)
    restored = next(iter(setup._states.values()))
    assert home.id == 501 and restored["section"] == "home" and restored["page"] == 3
    assert "Page 3 of" in home.message


@pytest.mark.asyncio
async def test_internal_section_back_restores_prior_home_page_once(real_control):
    adapter, setup, source, _homes = real_control
    await _open_home_page(adapter, setup, source, 2)
    await _click(adapter, source, _last_request(adapter).reply_markup, "Display")

    section = _last_request(adapter)
    labels = [button.text for row in section.reply_markup.rows for button in row.buttons]
    assert labels.count("← Back to setup") == 1
    await _click(adapter, source, section.reply_markup, "← Back to setup")

    home = _last_request(adapter)
    restored = next(iter(setup._states.values()))
    assert restored["section"] == "home" and restored["page"] == 2
    assert "Page 2 of" in home.message
    assert "Skills" in home.message and "Display" in home.message


@pytest.mark.asyncio
async def test_nested_dashboard_auth_back_returns_to_dashboard(real_control):
    adapter, setup, source, _homes = real_control
    await _open_home_page(adapter, setup, source, 3)
    await _click(adapter, source, _last_request(adapter).reply_markup, "Dashboard / Web UI")
    dashboard = _last_request(adapter)
    assert "Dashboard / WebUI" in dashboard.message

    await _click(adapter, source, dashboard.reply_markup, "Authentication")
    auth = _last_request(adapter)
    assert "Web UI authentication" in auth.message
    assert [button.text for row in auth.reply_markup.rows for button in row.buttons].count("← Back") == 1
    back = _payload(auth.reply_markup, "← Back")
    await _click(adapter, source, auth.reply_markup, "← Back")

    restored_dashboard = _last_request(adapter)
    assert restored_dashboard.id == 501
    assert "Dashboard / WebUI" in restored_dashboard.message
    labels = [button.text for row in restored_dashboard.reply_markup.rows for button in row.buttons]
    assert labels.count("Authentication") == 1
    assert labels.count("← Back to setup") == 1
    # The capability from the auth card was consumed by the first Back.
    replay = _callback()
    assert await adapter._handle_control_callback(replay, source, back)
    assert replay.answer.await_args.kwargs.get("alert") is True
    assert _last_request(adapter) is restored_dashboard


@pytest.mark.asyncio
async def test_setup_callback_token_replay_on_same_card_is_stale(real_control):
    adapter, setup, source, _homes = real_control
    await setup.handle_command(source)
    stale = _payload(_last_request(adapter).reply_markup, "Terminal")
    callback = _callback()
    assert await adapter._handle_control_callback(callback, source, stale)
    first_edit = _last_request(adapter)
    assert isinstance(first_edit, functions.messages.EditMessageRequest)
    assert "<h2>Terminal</h2>" in first_edit.message

    replay = _callback()
    assert await adapter._handle_control_callback(replay, source, stale)
    assert replay.answer.await_args.kwargs.get("alert") is True
    assert _last_request(adapter) is first_edit
    state = next(iter(setup._states.values()))
    assert state["section"] == "terminal"


@pytest.mark.asyncio
async def test_typed_input_replay_with_same_message_id_persists_once(real_control, tmp_path, monkeypatch):
    adapter, setup, source, homes = real_control
    await setup.handle_command(source)
    await _click(adapter, source, _last_request(adapter).reply_markup, "Terminal")
    await _click(adapter, source, _last_request(adapter).reply_markup, "Edit Working directory")
    assert next(iter(setup._states.values()))["pending_field"] == "terminal.cwd"

    from hermes_cli import config as config_module
    original_set = config_module.set_config_value
    writes = []

    def counted_set(key, value):
        writes.append((key, value))
        return original_set(key, value)

    monkeypatch.setattr(config_module, "set_config_value", counted_set)
    event = SimpleNamespace(message_id=9001)
    text = str(tmp_path)
    identity, pending = setup._pending_for_source(setup._home(source), source)
    assert identity in setup._pending and pending
    assert setup._allowed(source)
    assert await setup.handle_message(event, source, text)
    assert await setup.handle_message(event, source, text)
    assert [key for key, _value in writes] == ["terminal.cwd"]
    assert not setup._pending
    send_requests = [call.args[0] for call in adapter._client.await_args_list
                     if isinstance(call.args[0], functions.messages.SendMessageRequest)]
    assert len(send_requests) == 1

    from hermes_cli.config_effective import load_user_config_effective
    with setup._profile_scope(str(homes["alpha"])):
        config = load_user_config_effective(fail_closed=True)
    assert config["terminal"]["cwd"] == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_callbacks_reject_other_profile_even_for_same_owner_and_card(real_control):
    adapter, setup, source, _homes = real_control
    await setup.handle_command(source)
    stale = _payload(_last_request(adapter).reply_markup, "Terminal")
    other_profile = SimpleNamespace(**{**vars(source), "profile": "beta"})

    rejected = _callback()
    assert await adapter._handle_control_callback(rejected, other_profile, stale)
    assert rejected.answer.await_args.kwargs.get("alert") is True
    assert _last_request(adapter).__class__ is functions.messages.SendMessageRequest
    assert next(iter(setup._states.values()))["section"] == "home"


@pytest.mark.asyncio
async def test_mcp_editor_back_restores_exact_list_page_then_personal_origin(real_control):
    import yaml
    adapter, setup, source, homes = real_control
    (homes["alpha"] / "config.yaml").write_text(yaml.safe_dump({
        "mcp_servers": {f"server-{index:02}": {"command": "not-executed"} for index in range(20)},
    }))
    await _open_home_page(adapter, setup, source, 2)
    await _click(adapter, source, _last_request(adapter).reply_markup, "Personal settings and memory")
    ui = adapter._settings_ui()

    async def personal(action):
        payload = _settings_action_payload(_last_request(adapter), action)
        await adapter._invoke_control_ui_callback(_callback(), source, ui.handle_callback, payload)

    await personal(":mcp")
    assert "server-00" in _last_request(adapter).message, _last_request(adapter).message
    await personal(":mcp-page:1")
    await personal(":mcp-add")
    prompt = _last_request(adapter)
    assert "Add a custom MCP server" in prompt.message
    assert len([button for row in prompt.reply_markup.rows for button in row.buttons
                if button.text.startswith("← Back")]) == 1
    await _click(adapter, source, prompt.reply_markup, "← Back")
    restored = next(iter(ui._states.values()))
    assert restored["control_action"] == "mcp" and restored["control_page"] == 1
    await _click(adapter, source, _last_request(adapter).reply_markup, "← Back")
    assert "Personal settings" in _last_request(adapter).message
    await _click(adapter, source, _last_request(adapter).reply_markup, "← Back to setup")
    assert next(iter(setup._states.values()))["page"] == 2


@pytest.mark.asyncio
async def test_reopening_setup_invalidates_subordinate_callback_on_same_card(real_control):
    adapter, setup, source, _homes = real_control
    await _open_home_page(adapter, setup, source, 2)
    await _click(adapter, source, _last_request(adapter).reply_markup, "Personal settings and memory")
    stale = _settings_action_payload(_last_request(adapter), ":edit-general")
    ui = adapter._settings_ui()
    await setup.handle_command(source)
    reopened = _last_request(adapter)
    event = _callback()
    await adapter._invoke_control_ui_callback(event, source, ui.handle_callback, stale)
    assert _last_request(adapter) is reopened
    assert not ui._pending


@pytest.mark.asyncio
async def test_delegated_callback_profile_mismatch_never_invokes_controller(real_control):
    adapter, setup, source, _homes = real_control
    await _open_home_page(adapter, setup, source, 3)
    await _click(adapter, source, _last_request(adapter).reply_markup, "Browser")
    other = SimpleNamespace(**{**vars(source), "profile": "beta"})
    handler = AsyncMock()
    event = _callback()
    await adapter._invoke_control_ui_callback(event, other, handler, "not-dispatched")
    handler.assert_not_awaited()
    assert event.answer.await_args.kwargs.get("alert") is True


@pytest.mark.asyncio
async def test_reopening_setup_invalidates_only_its_bound_model_picker(real_control):
    adapter, setup, source, _homes = real_control
    await setup.handle_command(source)
    adapter._model_picker_state.update({
        1: {"message_id": "501", "chat_id": "101", "user_id": "101"},
        2: {"message_id": "999", "chat_id": "101", "user_id": "101"},
    })
    await setup.handle_command(source)
    assert 1 not in adapter._model_picker_state
    assert 2 in adapter._model_picker_state


@pytest.mark.asyncio
async def test_overview_row_and_pending_toolsets_survive_back_on_same_card(real_control):
    adapter, setup, source, homes = real_control
    import yaml
    config_path = homes["alpha"] / "config.yaml"
    config_path.write_text(yaml.safe_dump({"platform_toolsets": {"telegram": ["web"]}}))
    before = config_path.read_bytes()
    await setup.handle_command(source)
    home = _last_request(adapter)
    overview = [row for row in home.reply_markup.rows if any(button.text == "Overview" for button in row.buttons)]
    assert len(overview) == 1 and len(overview[0].buttons) == 1
    terminal = next(button for row in home.reply_markup.rows for button in row.buttons if button.text == "Terminal")
    assert not any((terminal.style.bg_primary, terminal.style.bg_success, terminal.style.bg_danger))
    await _click(adapter, source, home.reply_markup, "Overview")
    assert "Setup overview" in _last_request(adapter).message
    await _open_home_page(adapter, setup, source, 2)
    await _click(adapter, source, _last_request(adapter).reply_markup, "Tools")
    ui = adapter._setup_tools_ui()
    state = next(iter(ui._states.values()))
    state["page"] = next(index // 6 + 1 for index, row in enumerate(state["catalog"]) if row[0] == "video_gen")
    _text, markup = ui._list_view(state)
    # The real callback path, including route wrapping, is used for every click.
    await _click(adapter, source, markup, "Enable Video Generation")
    await _click(adapter, source, _last_request(adapter).reply_markup, "Review toolsets")
    await _click(adapter, source, _last_request(adapter).reply_markup, "← Back to setup")
    home = _last_request(adapter)
    assert "Pending changes" in home.message and "not saved" in home.message
    assert "next Telegram message turn" in home.message and "cached agent may be rebuilt" in home.message
    await _click(adapter, source, home.reply_markup, "Review pending changes")
    summary = _last_request(adapter)
    assert "Confirm toolset changes" in summary.message
    assert "Enable Video Generation" in summary.message
    await _click(adapter, source, summary.reply_markup, "Apply for next turn")
    assert "Saved and verified" in _last_request(adapter).message
    assert "video_gen" in yaml.safe_load(config_path.read_text())["platform_toolsets"]["telegram"]
    sends = [call.args[0] for call in adapter._client.await_args_list
             if isinstance(call.args[0], functions.messages.SendMessageRequest)]
    assert len(sends) == 1
    assert before != config_path.read_bytes()
