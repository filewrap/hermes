"""Behaviour contracts for ``gateway/allowgc.py``: the shared group profile.

An owner enabling /allowgc in a Telegram group gets one shared profile
(``gc-<chat id>``) with an enabled state, the real owner recorded, and access
toggles; disabling preserves it for seven days, after which the next lookup
reaps it. All state lives under a temp HERMES_HOME — never the real one.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gateway.allowgc import (
    ALLOWGC_PRESERVE_DAYS,
    card_message_id_for_chat,
    clear_opt_outs_for_user,
    create_group_profile,
    disable_group_profile,
    filter_turn_toolsets,
    is_opted_out,
    load_state,
    new_state,
    opt_out,
    profile_name_for_chat,
    resolve_home,
    save_state,
    set_card_message_id,
    set_toggle,
    toggles_for_chat,
)
from gateway.config import Platform
from gateway.session import SessionSource
from plugins.platforms.telegram.telegram_chat_settings import (
    load_chat_settings,
    update_chat_settings,
)

OWNER = "111"
MEMBER = "222"
CHAT = "-100123456"


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Temp hermes root: Path.home() + HERMES_HOME both point at tmp_path.

    Mirrors the profile-test pattern in tests/hermes_cli/test_profiles.py so
    ``hermes_cli.profiles`` resolves gc- profiles under the temp dir.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _group_source(chat_id=CHAT, user_id=MEMBER):
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="group", user_id=user_id)


def test_profile_name_for_chat_uses_absolute_id():
    assert profile_name_for_chat(-100123456) == "gc-100123456"
    assert profile_name_for_chat("100123456") == "gc-100123456"
    assert profile_name_for_chat("-100123456") == "gc-100123456"


def test_new_state_defaults_all_toggles_on():
    state = new_state(CHAT, "Group", OWNER)
    assert state["enabled"] is True
    assert state["disabled_at"] is None
    assert state["chat_id"] == CHAT
    assert state["chat_name"] == "Group"
    assert state["owner_id"] == OWNER
    assert state["toggles"] == {"tools": True, "web": True, "shell": True, "trigger": True}
    assert state["opted_out"] == []


def test_new_state_honours_given_toggles():
    state = new_state(CHAT, "Group", OWNER, toggles={"web": False})
    assert state["toggles"]["web"] is False
    assert state["toggles"]["tools"] is True


def test_save_load_round_trip_and_missing_state(tmp_path):
    home = tmp_path / "gc-home"
    assert load_state(home) is None
    state = new_state(CHAT, "Group", OWNER)
    save_state(home, state)
    assert load_state(home) == state
    (home / "allowgc.json").write_text("{not json", encoding="utf-8")
    assert load_state(home) is None


def test_card_message_id_defaults_none_and_round_trips(profile_env):
    home = create_group_profile(CHAT, "Group", OWNER)
    assert load_state(home)["card_message_id"] is None
    assert card_message_id_for_chat(CHAT) is None
    assert card_message_id_for_chat("-100999") is None

    assert set_card_message_id(home, 55)["card_message_id"] == "55"
    assert load_state(home)["card_message_id"] == "55"
    assert card_message_id_for_chat(CHAT) == "55"

    # Disabling and re-enabling keeps the stored card id.
    disable_group_profile(CHAT)
    create_group_profile(CHAT, "Group", OWNER)
    assert load_state(home)["card_message_id"] == "55"

    # A missing profile has nothing to record into.
    assert set_card_message_id(profile_env / "gc-missing", 55) is None


def test_create_group_profile_seeds_dir_env_and_state(profile_env):
    home = create_group_profile(CHAT, "Group", OWNER)
    assert home.is_dir()
    assert home.name == "gc-100123456"
    env = (home / ".env").read_text(encoding="utf-8")
    assert "OWNER_TELEGRAM_ID=111" in env
    state = load_state(home)
    assert state["enabled"] is True
    assert state["owner_id"] == OWNER
    assert state["chat_id"] == CHAT
    assert state["chat_name"] == "Group"
    assert resolve_home(CHAT, reap=False) == home


def test_create_group_profile_with_toggles(profile_env):
    home = create_group_profile(CHAT, "Group", OWNER, toggles={"shell": False})
    assert toggles_for_chat(CHAT)["shell"] is False
    assert toggles_for_chat(CHAT)["tools"] is True
    assert load_state(home)["toggles"]["shell"] is False


def test_create_group_profile_is_idempotent_and_reenables(profile_env):
    first = create_group_profile(CHAT, "Group", OWNER)
    disabled = disable_group_profile(CHAT)
    assert disabled["enabled"] is False
    assert disabled["disabled_at"]
    assert resolve_home(CHAT, reap=False) is None
    second = create_group_profile(CHAT, "Group", OWNER)
    assert second == first
    state = load_state(first)
    assert state["enabled"] is True
    assert state["disabled_at"] is None
    assert resolve_home(CHAT, reap=False) == first


def test_trigger_word_survives_allowgc_off_and_on(profile_env):
    """Ruling 2026-11-17: /trigger and /allowgc are different features that work
    together — turning /allowgc off and back on must not clear the group's
    trigger word/mode. The trigger lives in the group profile's config.yaml,
    which neither the disable nor the re-enable path writes."""
    home = create_group_profile(CHAT, "Group", OWNER)
    update_chat_settings(home, "group", CHAT,
                         {"trigger_word": "emilia", "trigger_mode": "trigger"})
    assert load_chat_settings(home, "group", CHAT)["trigger_word"] == "emilia"

    disable_group_profile(CHAT)
    assert load_chat_settings(home, "group", CHAT)["trigger_word"] == "emilia"

    create_group_profile(CHAT, "Group", OWNER)
    settings = load_chat_settings(home, "group", CHAT)
    assert settings["trigger_word"] == "emilia"
    assert settings["trigger_mode"] == "trigger"


def test_disable_group_profile_unknown_chat_returns_none(profile_env):
    assert disable_group_profile("-100999") is None


def test_resolve_home_reaps_profile_disabled_over_seven_days(profile_env):
    home = create_group_profile(CHAT, "Group", OWNER)
    disable_group_profile(CHAT)
    state = load_state(home)
    old = datetime.now(UTC) - timedelta(days=ALLOWGC_PRESERVE_DAYS + 1)
    state["disabled_at"] = old.isoformat()
    save_state(home, state)
    assert resolve_home(CHAT) is None
    assert not home.exists()


def test_resolve_home_keeps_profile_disabled_under_seven_days(profile_env):
    home = create_group_profile(CHAT, "Group", OWNER)
    disable_group_profile(CHAT)
    state = load_state(home)
    recent = datetime.now(UTC) - timedelta(days=1)
    state["disabled_at"] = recent.isoformat()
    save_state(home, state)
    assert resolve_home(CHAT) is None
    assert home.exists()
    assert load_state(home)["enabled"] is False


def test_opt_out_and_clear_opt_outs(profile_env):
    home = create_group_profile(CHAT, "Group", OWNER)
    assert is_opted_out(home, MEMBER) is False
    opt_out(home, MEMBER)
    assert is_opted_out(home, MEMBER) is True
    assert is_opted_out(home, OWNER) is False
    opt_out(home, MEMBER)  # second opt-out is a no-op, not a duplicate
    assert load_state(home)["opted_out"] == [MEMBER]
    changed = clear_opt_outs_for_user(MEMBER)
    assert changed == [home]
    assert is_opted_out(home, MEMBER) is False
    assert clear_opt_outs_for_user(MEMBER) == []


def test_clear_opt_outs_only_touches_profiles_with_the_user(profile_env):
    home_a = create_group_profile("-100111", "A", OWNER)
    home_b = create_group_profile("-100222", "B", OWNER)
    opt_out(home_a, MEMBER)
    assert clear_opt_outs_for_user(MEMBER) == [home_a]
    assert is_opted_out(home_b, MEMBER) is False


def test_filter_turn_toolsets_owner_exempt(profile_env):
    create_group_profile(CHAT, "Group", OWNER, toggles={"tools": False})
    owner_source = _group_source(user_id=OWNER)
    assert filter_turn_toolsets(["terminal", "web", "browser"], owner_source) == [
        "terminal", "web", "browser"]


def test_filter_turn_toolsets_tools_off_removes_everything(profile_env):
    create_group_profile(CHAT, "Group", OWNER)
    set_toggle(resolve_home(CHAT, reap=False), "tools", False)
    assert filter_turn_toolsets(["terminal", "web", "browser"], _group_source()) == []


def test_filter_turn_toolsets_shell_off_drops_terminal_only(profile_env):
    create_group_profile(CHAT, "Group", OWNER)
    set_toggle(resolve_home(CHAT, reap=False), "shell", False)
    assert filter_turn_toolsets(
        ["terminal", "web", "browser", "file"], _group_source()) == ["web", "browser", "file"]


def test_filter_turn_toolsets_web_off_drops_web_and_browser(profile_env):
    create_group_profile(CHAT, "Group", OWNER)
    set_toggle(resolve_home(CHAT, reap=False), "web", False)
    assert filter_turn_toolsets(
        ["terminal", "web", "browser", "file"], _group_source()) == ["terminal", "file"]


def test_filter_turn_toolsets_non_allowgc_source_unchanged(profile_env):
    create_group_profile(CHAT, "Group", OWNER)
    set_toggle(resolve_home(CHAT, reap=False), "tools", False)
    # Same toggles-off group, but a chat with no group profile: untouched.
    assert filter_turn_toolsets(
        ["terminal", "web"], _group_source(chat_id="-100999")) == ["terminal", "web"]
    # A DM to the allowgc chat id is not a group source: untouched.
    dm = SessionSource(
        platform=Platform.TELEGRAM, chat_id=CHAT, chat_type="dm", user_id=MEMBER)
    assert filter_turn_toolsets(["terminal", "web"], dm) == ["terminal", "web"]


def test_filter_turn_toolsets_empty_and_none_pass_through(profile_env):
    create_group_profile(CHAT, "Group", OWNER)
    assert filter_turn_toolsets([], _group_source()) == []
    assert filter_turn_toolsets(None, _group_source()) == []
