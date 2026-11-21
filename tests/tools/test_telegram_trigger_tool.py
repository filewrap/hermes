"""Behaviour contracts for the telegram_trigger tool.

The owner changes a group's invocation from plain words; the tool writes the
SAME store the /trigger command writes (the profile config.yaml
``platforms.telegram.chat_access.groups.<chat_id>`` record) and the adapter's
``_group_triggered`` reads the change on the next message. Tests run against
a temp HERMES_HOME; no live Telegram, no LLM calls.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from plugins.platforms.telegram.telegram_chat_settings import load_chat_settings

# Imported for its registration side effect.
import tools.telegram_trigger_tool
from tools.telegram_trigger_tool import telegram_trigger

OWNER_ID = "100"
OTHER_ID = "777"
GROUP = "-100123"


@pytest.fixture
def home_env(tmp_path, monkeypatch):
    """Temp Hermes home: Path.home and HERMES_HOME both point inside tmp."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def group_session(home_env):
    tokens = set_session_vars(
        platform="telegram", chat_id=GROUP, chat_type="group", user_id=OWNER_ID)
    try:
        yield home_env
    finally:
        clear_session_vars(tokens)


def _owner_agent(platform="telegram"):
    # A Telegram group turn trusts the adapter's catalog filter for the
    # owner gate; the handler re-check passes here by design.
    return SimpleNamespace(platform=platform)


def _call(args, agent=None):
    return json.loads(telegram_trigger(
        args, parent_agent=agent if agent is not None else _owner_agent()))


def test_no_args_reports_current_settings(group_session):
    result = _call({})
    assert result["chat_id"] == GROUP
    assert result["trigger_mode"] == ""
    assert result["trigger_word"] == ""


def test_mode_trigger_plus_word_writes_the_trigger_command_store(group_session):
    result = _call({"mode": "trigger", "trigger_word": "xyz"})
    assert result["trigger_mode"] == "trigger"
    assert result["trigger_word"] == "xyz"
    assert result["changed"] == ["trigger_mode", "trigger_word"]
    assert "next message" in result["note"]
    stored = load_chat_settings(group_session, "group", GROUP)
    assert stored["trigger_mode"] == "trigger"
    assert stored["trigger_word"] == "xyz"


def test_switch_back_to_mention_keeps_the_word_inert(group_session):
    _call({"mode": "trigger", "trigger_word": "xyz"})
    result = _call({"mode": "tag"})
    assert result["trigger_mode"] == "tag"
    stored = load_chat_settings(group_session, "group", GROUP)
    assert stored["trigger_mode"] == "tag"
    assert stored["trigger_word"] == "xyz"  # kept, inert under tag mode


def test_empty_word_disables_the_word(group_session):
    _call({"mode": "trigger", "trigger_word": "xyz"})
    result = _call({"trigger_word": ""})
    assert result["trigger_word"] == ""
    assert load_chat_settings(group_session, "group", GROUP)["trigger_word"] == ""


def test_direct_mode_needs_explicit_confirmation(group_session):
    result = _call({"mode": "direct"})
    assert "error" in result
    assert "Confirm it explicitly" in result["error"]
    assert load_chat_settings(group_session, "group", GROUP)["trigger_mode"] == ""
    result = _call({"mode": "direct", "confirm_direct": True})
    assert result["trigger_mode"] == "direct"
    assert load_chat_settings(group_session, "group", GROUP)["trigger_mode"] == "direct"


def test_invalid_mode_and_word_are_refused_plainly(group_session):
    assert "error" in _call({"mode": "shout"})
    assert "error" in _call({"trigger_word": "two words"})
    assert "error" in _call({"trigger_word": "x" * 65})
    assert "error" in _call({"trigger_word": 42})
    stored = load_chat_settings(group_session, "group", GROUP)
    assert stored["trigger_mode"] == "" and stored["trigger_word"] == ""


def test_dm_refuses_and_writes_nothing(home_env):
    tokens = set_session_vars(
        platform="telegram", chat_id=OWNER_ID, chat_type="dm", user_id=OWNER_ID)
    try:
        result = _call({"mode": "trigger", "trigger_word": "xyz"})
    finally:
        clear_session_vars(tokens)
    assert "error" in result
    assert "per-group" in result["error"]
    assert load_chat_settings(home_env, "group", GROUP)["trigger_word"] == ""


def test_non_telegram_platform_refuses(home_env):
    tokens = set_session_vars(platform="cli", chat_id="cli", chat_type="dm", user_id="")
    try:
        result = _call({"mode": "trigger", "trigger_word": "xyz"})
    finally:
        clear_session_vars(tokens)
    assert "error" in result
    assert "Telegram group" in result["error"]


def test_tenant_scope_denied(group_session):
    from agent.tenant_scope import TenantScope, reset_tenant_scope, set_tenant_scope

    scope = TenantScope(group_session, object(), "room-a", group_session / "a", "tg-a")
    token = set_tenant_scope(scope)
    try:
        result = _call({"mode": "trigger", "trigger_word": "xyz"})
    finally:
        reset_tenant_scope(token)
    assert "error" in result
    assert "Only the owner" in result["error"]
    assert load_chat_settings(group_session, "group", GROUP)["trigger_word"] == ""


def test_dm_non_owner_denied(group_session):
    from gateway.telegram_personal_settings import (
        reset_personal_memory_context,
        set_personal_memory_context,
    )

    (group_session / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
    agent = SimpleNamespace(platform="telegram")
    token = set_personal_memory_context(group_session, OTHER_ID)
    try:
        result = _call({"mode": "trigger", "trigger_word": "xyz"}, agent=agent)
    finally:
        reset_personal_memory_context(token)
    assert "error" in result
    assert "Only the owner" in result["error"]
    assert load_chat_settings(group_session, "group", GROUP)["trigger_word"] == ""


def test_tool_is_registered_in_the_telegram_admin_toolset():
    from toolsets import TOOLSETS
    from tools.registry import registry

    entry = registry.get_entry("telegram_trigger")
    assert entry is not None
    assert entry.toolset == "telegram_admin"
    assert "telegram_trigger" in TOOLSETS["telegram_admin"]["tools"]
    assert "telegram_trigger" in TOOLSETS["hermes-telegram"]["tools"]
