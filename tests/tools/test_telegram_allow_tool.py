"""Behaviour contracts for the telegram_allow tool.

The owner admits or removes Telegram users, groups and channels from plain
words; the tool calls the SAME store functions the access UI calls
(``allow_user`` globally, ``grant_scope_access`` / ``revoke_scope_access``
in a group, allowgc's group-profile enable/disable for a whole chat) plus
the same live adapter allowlist sync. The owner's words are the
confirmation — no card, no round-trip. Only a resolution failure asks the
owner to say it again. Tests run against a temp HERMES_HOME; no live
Telegram, no LLM calls.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.session_context import clear_session_vars, set_session_vars

# Imported for its registration side effect.
import tools.telegram_allow_tool
from tools.telegram_allow_tool import telegram_allow

OWNER_ID = "100"
OTHER_ID = "777"
TARGET_ID = "555"
TARGET_USERNAME = "someuser"
GROUP = "-100123"


@pytest.fixture
def home_env(tmp_path, monkeypatch):
    """Temp Hermes home: Path.home and HERMES_HOME both point inside tmp."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n")
    return home


@pytest.fixture
def owner_session(home_env):
    tokens = set_session_vars(
        platform="telegram", chat_id=OWNER_ID, chat_type="dm", user_id=OWNER_ID)
    try:
        yield home_env
    finally:
        clear_session_vars(tokens)


def _owner_agent(platform="telegram"):
    # A Telegram turn trusts the adapter's catalog filter for the owner
    # gate; the handler re-check passes here by design.
    return SimpleNamespace(platform=platform)


def _call(args, agent=None):
    return json.loads(telegram_allow(
        args, parent_agent=agent if agent is not None else _owner_agent()))


def _launch_store(home_env):
    from gateway.mongo_multiuser import store_for_home

    return store_for_home(home_env)


def _start_user(home_env, user_id, username=None):
    store = _launch_store(home_env)
    store.start_user(user_id)
    if username is not None:
        store.record_started_identity(user_id, username, "Some User")
    return store


def test_global_allow_by_numeric_id_writes_the_access_store(owner_session):
    result = _call({"target": TARGET_ID, "action": "allow"})
    assert result["kind"] == "user"
    assert result["scope"] == "global"
    assert _launch_store(owner_session).get_access(TARGET_ID)["allowed"] is True


def test_global_disallow_removes_the_grant(owner_session):
    _call({"target": TARGET_ID, "action": "allow"})
    result = _call({"target": TARGET_ID, "action": "disallow"})
    assert result["scope"] == "global"
    assert _launch_store(owner_session).get_access(TARGET_ID)["allowed"] is False


def test_username_resolves_only_a_started_user(owner_session):
    _start_user(owner_session, TARGET_ID, TARGET_USERNAME)
    result = _call({"target": f"@{TARGET_USERNAME}", "action": "allow"})
    assert result["target"] == TARGET_ID
    assert _launch_store(owner_session).get_access(TARGET_ID)["allowed"] is True


def test_unknown_username_is_a_resolution_failure(owner_session):
    result = _call({"target": "@neverstarted", "action": "allow"})
    assert "error" in result
    assert "Could not resolve" in result["error"]
    assert result["needs_owner_confirmation"] is True
    assert _launch_store(owner_session).get_access(TARGET_ID)["allowed"] is False


def test_bare_group_name_cannot_resolve(owner_session):
    result = _call({"target": "Testing Agents", "action": "allow"})
    assert "error" in result
    assert "Could not resolve" in result["error"]
    assert result["needs_owner_confirmation"] is True


def test_group_id_target_enables_the_whole_chat(owner_session):
    from gateway.allowgc import is_active_for_chat, load_state, resolve_home

    result = _call({"target": GROUP, "action": "allow"})
    assert result["kind"] == "group"
    assert result["chat_id"] == GROUP
    assert is_active_for_chat(GROUP) is True
    assert load_state(resolve_home(GROUP, reap=False))["enabled"] is True


def test_group_id_target_disallow_turns_shared_access_off(owner_session):
    from gateway.allowgc import is_active_for_chat

    _call({"target": GROUP, "action": "allow"})
    result = _call({"target": GROUP, "action": "disallow"})
    assert result["kind"] == "group"
    assert is_active_for_chat(GROUP) is False


def test_user_at_group_scope_grants_only_there(owner_session):
    from gateway.allowgc import create_group_profile
    from gateway.mongo_multiuser import principal, store_for_home

    create_group_profile(GROUP, None, OWNER_ID)
    result = _call({"target": TARGET_ID, "action": "allow", "scope": GROUP})
    assert result["scope"] == GROUP
    from gateway.allowgc import resolve_home

    group_store = store_for_home(resolve_home(GROUP, reap=False))
    access = group_store.access_for_scope(TARGET_ID, "group", GROUP)
    assert access["allowed"] is True
    assert _launch_store(owner_session).get_access(TARGET_ID)["allowed"] is False

    result = _call({"target": TARGET_ID, "action": "disallow", "scope": GROUP})
    assert result["scope"] == GROUP
    assert group_store.access_for_scope(TARGET_ID, "group", GROUP)["allowed"] is False
    # The scoped revoke never touches the global grant.
    _call({"target": TARGET_ID, "action": "allow"})
    _call({"target": TARGET_ID, "action": "disallow", "scope": GROUP})
    assert _launch_store(owner_session).get_access(TARGET_ID)["allowed"] is True


def test_scoped_grant_without_a_group_profile_fails_plainly(owner_session):
    result = _call({"target": TARGET_ID, "action": "allow", "scope": GROUP})
    assert "error" in result
    assert "no shared profile" in result["error"]
    assert "Allow the whole chat first" in result["error"]
    assert _launch_store(owner_session).get_access(TARGET_ID)["allowed"] is False


def test_owner_can_never_revoke_themselves(owner_session):
    result = _call({"target": OWNER_ID, "action": "disallow"})
    assert "error" in result
    assert "Owner access cannot be changed" in result["error"]
    assert _launch_store(owner_session).get_access(OWNER_ID)["allowed"] is True


def test_group_id_as_user_is_never_a_user_grant(owner_session):
    from gateway.allowgc import is_active_for_chat

    result = _call({"target": GROUP, "action": "allow", "scope": "global"})
    assert result["kind"] == "group"
    assert is_active_for_chat(GROUP) is True


def test_non_telegram_platform_refuses(owner_session):
    tokens = set_session_vars(platform="cli", chat_id="cli", chat_type="dm", user_id="")
    try:
        result = _call({"target": TARGET_ID, "action": "allow"}, agent=_owner_agent("cli"))
    finally:
        clear_session_vars(tokens)
    assert "error" in result
    assert "Telegram" in result["error"]
    assert _launch_store(owner_session).get_access(TARGET_ID)["allowed"] is False


def test_tenant_scope_denied(owner_session):
    from agent.tenant_scope import TenantScope, reset_tenant_scope, set_tenant_scope

    scope = TenantScope(owner_session, object(), "room-a", owner_session / "a", "tg-a")
    token = set_tenant_scope(scope)
    try:
        result = _call({"target": TARGET_ID, "action": "allow"})
    finally:
        reset_tenant_scope(token)
    assert "error" in result
    assert "Only the owner" in result["error"]
    assert _launch_store(owner_session).get_access(TARGET_ID)["allowed"] is False


def test_dm_non_owner_denied(owner_session):
    from gateway.telegram_personal_settings import (
        reset_personal_memory_context,
        set_personal_memory_context,
    )

    agent = SimpleNamespace(platform="telegram")
    token = set_personal_memory_context(owner_session, OTHER_ID)
    try:
        result = _call({"target": TARGET_ID, "action": "allow"}, agent=agent)
    finally:
        reset_personal_memory_context(token)
    assert "error" in result
    assert "Only the owner" in result["error"]
    assert _launch_store(owner_session).get_access(TARGET_ID)["allowed"] is False


def test_tool_is_registered_in_the_telegram_admin_toolset():
    from toolsets import TOOLSETS
    from tools.registry import registry

    entry = registry.get_entry("telegram_allow")
    assert entry is not None
    assert entry.toolset == "telegram_admin"
    assert "telegram_allow" in TOOLSETS["telegram_admin"]["tools"]
    assert "telegram_allow" in TOOLSETS["hermes-telegram"]["tools"]


def test_non_owner_never_sees_the_tool(owner_session):
    pytest.importorskip("telethon")
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = TelethonAdapter(PlatformConfig())
    owner_source = SimpleNamespace(user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm")
    assert adapter.disabled_tools_for_source(owner_source) is None
    other_source = SimpleNamespace(user_id=OTHER_ID, chat_id=OTHER_ID, chat_type="dm")
    assert "telegram_allow" in adapter.disabled_tools_for_source(other_source)
    group_source = SimpleNamespace(user_id=OTHER_ID, chat_id=GROUP, chat_type="group")
    assert "telegram_allow" in adapter.disabled_tools_for_source(group_source)
