"""Telegram operations keep the trusted actor, topic, profile and transport route."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_context import set_session_vars, clear_session_vars


def _adapter(home):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-10042", chat_type="group",
                           user_id="100", thread_id="77")
    adapter = SimpleNamespace(
        _client=object(), build_source=lambda *_a, **_kw: source,
        _settings_home=lambda _s: home, _authorized=lambda _s: True,
        _is_owner_user=lambda _u, **_kw: True,
        request_telegram_operation=AsyncMock(return_value={"pending_confirmation": True}),
    )
    return source, adapter


def _dispatch(monkeypatch, tmp_path, args, **session):
    import tools.send_message_senders as senders
    from model_tools import handle_function_call
    source, adapter = _adapter(tmp_path)
    runner = SimpleNamespace(_gateway_loop=None, _get_cached_session_source=lambda _k: source)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(senders, "_live_adapter", lambda _platform: (runner, adapter))
    fields = dict(platform="telegram", chat_id="-10042", chat_type="group", user_id="100",
                  thread_id="77", session_key="test-session")
    fields.update(session)
    tokens = set_session_vars(**fields)
    try:
        result = json.loads(handle_function_call("telegram_client", args))
    finally:
        clear_session_vars(tokens)
    return result, source, adapter


def test_tool_registry_routes_current_session_without_faking_confirmation(monkeypatch, tmp_path):
    result, source, adapter = _dispatch(monkeypatch, tmp_path,
                                       {"action": "poll_send", "params": {"question": "Choose"}})
    assert result == {"pending_confirmation": True}
    adapter.request_telegram_operation.assert_awaited_once_with(source, "poll_send", {"question": "Choose"})


@pytest.mark.parametrize("fields", [{"platform": "discord"}, {"user_id": ""}, {"chat_id": ""},
                                    {"user_id": "200"}, {"thread_id": "88"}])
def test_invalid_or_stale_session_never_calls_transport(monkeypatch, tmp_path, fields):
    result, _source, adapter = _dispatch(monkeypatch, tmp_path, {"action": "capabilities"}, **fields)
    assert "error" in result
    adapter.request_telegram_operation.assert_not_awaited()


@pytest.mark.parametrize("field", ["chat_id", "peer", "destination", "profile", "confirmed", "confirmation_token"])
def test_model_cannot_supply_route_or_approval(monkeypatch, tmp_path, field):
    result, _source, adapter = _dispatch(monkeypatch, tmp_path,
                                       {"action": "poll_send", "params": {field: "elsewhere"}})
    assert "error" in result
    adapter.request_telegram_operation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["profile", "bot", "owner", "authorization", "route"])
async def test_runtime_rechecks_every_privilege_before_rpc(monkeypatch, tmp_path, rejection):
    from tools.telegram_client_tool import _request_for_session
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source, adapter = _adapter(tmp_path)
    runner = SimpleNamespace(_get_cached_session_source=lambda _k: source)
    if rejection == "profile":
        adapter._settings_home = lambda _s: tmp_path / "another-profile"
    elif rejection == "bot":
        source.is_bot = True
    elif rejection == "owner":
        adapter._is_owner_user = lambda *_a, **_kw: False
    elif rejection == "authorization":
        adapter._authorized = lambda _s: False
    else:
        source.profile_route_rejected = True
    tokens = set_session_vars(platform="telegram", chat_id="-10042", user_id="100", thread_id="77", session_key="s")
    try:
        result = await _request_for_session(runner, adapter, "-10042", "100", "capabilities", {})
    finally:
        clear_session_vars(tokens)
    assert "error" in result
    adapter.request_telegram_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_admitted_nonowner_can_send_rich_message_only_in_active_chat_topic(tmp_path, monkeypatch):
    from tools.telegram_client_tool import _request_for_session
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source, adapter = _adapter(tmp_path)
    source.user_id = "200"
    adapter._is_owner_user = lambda *_a, **_kw: False
    runner = SimpleNamespace(_get_cached_session_source=lambda _k: source)
    tokens = set_session_vars(platform="telegram", chat_id="-10042", user_id="200",
                              thread_id="77", session_key="nonowner-session")
    params = {"content": "A rich reply for this topic"}
    try:
        sent = await _request_for_session(runner, adapter, "-10042", "200", "rich_message_send", params)
        assert sent == {"pending_confirmation": True}
        adapter.request_telegram_operation.assert_awaited_once_with(source, "rich_message_send", params)
        assert source.chat_id == "-10042" and source.thread_id == "77" and source.user_id == "200"
        for action in ("capabilities", "chat_title"):
            denied = await _request_for_session(runner, adapter, "-10042", "200", action, {})
            assert "error" in denied
        adapter.request_telegram_operation.assert_awaited_once()
    finally:
        clear_session_vars(tokens)


@pytest.mark.asyncio
async def test_admitted_nonowner_can_quote_reply_only_in_active_chat_topic(tmp_path, monkeypatch):
    from tools.telegram_client_tool import _request_for_session
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source, adapter = _adapter(tmp_path)
    source.user_id = "200"
    adapter._is_owner_user = lambda *_a, **_kw: False
    runner = SimpleNamespace(_get_cached_session_source=lambda _k: source)
    tokens = set_session_vars(platform="telegram", chat_id="-10042", user_id="200",
                              thread_id="77", session_key="nonowner-session")
    params = {"message_id": 9, "quote": "quoted excerpt", "content": "Replying to that part."}
    try:
        sent = await _request_for_session(runner, adapter, "-10042", "200", "quote_reply", params)
        assert sent == {"pending_confirmation": True}
        adapter.request_telegram_operation.assert_awaited_once_with(source, "quote_reply", params)
    finally:
        clear_session_vars(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["actor", "chat", "topic", "profile", "bot", "not_admitted"])
async def test_nonowner_rich_send_rejects_unadmitted_or_cross_scope_route(monkeypatch, tmp_path, mismatch):
    from tools.telegram_client_tool import _request_for_session
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source, adapter = _adapter(tmp_path)
    source.user_id = "200"
    adapter._is_owner_user = lambda *_a, **_kw: False
    runner = SimpleNamespace(_get_cached_session_source=lambda _k: source)
    active_chat, active_user = "-10042", "200"
    thread_id = "77"
    if mismatch == "actor":
        active_user = "201"
    elif mismatch == "chat":
        active_chat = "-10043"
    elif mismatch == "topic":
        thread_id = "78"
    elif mismatch == "profile":
        adapter._settings_home = lambda _s: tmp_path / "other-profile"
    elif mismatch == "bot":
        source.is_bot = True
    else:
        adapter._authorized = lambda _s: False
    tokens = set_session_vars(platform="telegram", chat_id="-10042", user_id="200",
                              thread_id=thread_id, session_key="nonowner-session")
    try:
        result = await _request_for_session(runner, adapter, active_chat, active_user,
                                            "rich_message_send", {"content": "No cross-scope send"})
    finally:
        clear_session_vars(tokens)
    assert "error" in result
    adapter.request_telegram_operation.assert_not_awaited()


def test_surface_toolset_and_native_commands_are_complete():
    from hermes_cli.commands_platforms import telegram_bot_commands, telegram_menu_commands
    from toolsets import get_toolset, _HERMES_CORE_TOOLS
    from plugins.platforms.telegram.operations_routing import TELEGRAM_OPERATION_COMMANDS
    from tools.telegram_client_tool import TELEGRAM_CLIENT_SCHEMA
    assert "telegram_client" in get_toolset("hermes-telegram")["tools"]
    assert "telegram_client" not in _HERMES_CORE_TOOLS
    assert TELEGRAM_OPERATION_COMMANDS <= {name for name, _ in telegram_bot_commands(include_plugins=False)}
    menu, _overflow = telegram_menu_commands()
    assert TELEGRAM_OPERATION_COMMANDS <= {name for name, _ in menu}
    assert "chat_id" not in TELEGRAM_CLIENT_SCHEMA["parameters"]["properties"]


def test_session_cache_preserves_wire_invisible_transport_identity(tmp_path):
    from gateway.run import GatewayRunner
    from gateway.session_identity import RoutingIdentity, identity_of
    source, adapter = _adapter(tmp_path)
    identity = RoutingIdentity(transport_profile="primary", runtime_profile="satellite",
        authorization_home=tmp_path / "primary", runtime_home=tmp_path,
        transport=lambda: adapter)
    source._identity = identity
    source._transport_adapter_ref = lambda: adapter
    source._authorization_profile_home = tmp_path / "primary"
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._cache_session_source("s", source)
    copied = runner._get_cached_session_source("s")
    assert copied is not source and identity_of(copied) is identity
    assert copied._transport_adapter_ref() is adapter
    assert copied._authorization_profile_home == tmp_path / "primary"


def test_tool_uses_receiving_bot_not_active_runtime_profile_bot(monkeypatch, tmp_path):
    from gateway.session_identity import RoutingIdentity
    from model_tools import handle_function_call
    import tools.send_message_senders as senders
    source, receiving = _adapter(tmp_path)
    _, wrong = _adapter(tmp_path)
    source._identity = RoutingIdentity(transport_profile="primary", runtime_profile="satellite",
        authorization_home=tmp_path / "primary", runtime_home=tmp_path, transport=lambda: receiving)
    runner = SimpleNamespace(_gateway_loop=None, _get_cached_session_source=lambda _k: source)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(senders, "_live_adapter", lambda _p: (runner, wrong))
    tokens = set_session_vars(platform="telegram", chat_id="-10042", user_id="100",
                              thread_id="77", session_key="s")
    try:
        result = json.loads(handle_function_call("telegram_client", {"action": "capabilities"}))
    finally:
        clear_session_vars(tokens)
    assert result == {"pending_confirmation": True}
    receiving.request_telegram_operation.assert_awaited_once()
    wrong.request_telegram_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_pinned_transport_never_falls_back_to_another_bot(monkeypatch, tmp_path):
    from gateway.session_identity import RoutingIdentity
    from tools.telegram_client_tool import _request_for_session
    source, adapter = _adapter(tmp_path)
    source._identity = RoutingIdentity(transport_profile="primary", runtime_profile="satellite",
        authorization_home=tmp_path / "primary", runtime_home=tmp_path)
    runner = SimpleNamespace(_get_cached_session_source=lambda _k: source)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = set_session_vars(platform="telegram", chat_id="-10042", user_id="100", thread_id="77", session_key="s")
    try:
        result = await _request_for_session(runner, adapter, "-10042", "100", "capabilities", {})
    finally:
        clear_session_vars(tokens)
    assert "error" in result
    adapter.request_telegram_operation.assert_not_awaited()


def test_bundled_telegram_skills_and_static_prompt_are_connected():
    from pathlib import Path
    from tools.skills_sync import _discover_bundled_skills
    from agent.system_prompt import _default_platform_hint
    from gateway.telegram_instructions import TELEGRAM_OPERATIONS_INSTRUCTIONS
    repo = Path(__file__).resolve().parents[2]
    found = dict(_discover_bundled_skills(repo / "skills"))
    assert {"telegram-client", "telegram-bots"} <= found.keys()
    # The hint lives on the Telegram plugin's PlatformEntry now; resolve it the
    # way the system prompt does.
    hint = _default_platform_hint("telegram")
    assert TELEGRAM_OPERATIONS_INSTRUCTIONS in hint
    assert "telegram-client" in hint and "telegram-bots" in hint
    assert "confirmation_required" in TELEGRAM_OPERATIONS_INSTRUCTIONS


def test_capability_parameters_cover_every_client_action_without_route_overrides():
    from plugins.platforms.telegram.client_operations import ALL_ACTIONS, CLIENT_ACTION_PARAMETERS
    assert set(CLIENT_ACTION_PARAMETERS) == ALL_ACTIONS - {"capabilities"}
    assert all("chat_id" not in parameters and "confirmed" not in parameters
               for parameters in CLIENT_ACTION_PARAMETERS.values())
