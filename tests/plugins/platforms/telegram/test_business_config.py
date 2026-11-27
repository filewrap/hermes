"""Behaviour contracts for the Telegram Business Mode config helper.

``business_config`` is the ONE reader/writer the UI worker and the tool share
for ``platforms.telegram.business``: reads merge the profile's config over
the section-10 defaults, writes go through the atomic config writer and
never delete by omission. Tests run against a temp HERMES_HOME; no live
Telegram.
"""
from pathlib import Path

import pytest

from hermes_cli.config import read_user_config_raw
from hermes_cli.config_defaults import DEFAULT_CONFIG
from plugins.platforms.telegram.business_config import (
    BUSINESS_DEFAULTS,
    load_business_settings,
    save_business_settings,
)
from plugins.platforms.telegram.business_manager import DEFAULT_PERSONA


@pytest.fixture
def home_env(tmp_path, monkeypatch):
    """Temp Hermes home: Path.home and HERMES_HOME both point inside tmp."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_declared_defaults_match_the_design_section_10():
    assert DEFAULT_CONFIG["telegram"]["business"] == BUSINESS_DEFAULTS
    assert BUSINESS_DEFAULTS == {
        "enabled": True,
        "trigger_mode": "direct",
        "trigger_word": "",
        "debounce_seconds": 8,
        "max_customer_text_chars": 4000,
        "owner_persona": DEFAULT_PERSONA,
    }


def test_load_returns_defaults_when_nothing_is_configured(home_env):
    assert load_business_settings(home_env) == BUSINESS_DEFAULTS


def test_load_prefers_the_platform_namespace_over_the_top_level_block(home_env):
    (home_env / "config.yaml").write_text(
        "telegram:\n"
        "  business:\n"
        "    trigger_mode: trigger\n"
        "    trigger_word: top\n"
        "platforms:\n"
        "  telegram:\n"
        "    business:\n"
        "      trigger_word: platform\n",
        encoding="utf-8",
    )
    settings = load_business_settings(home_env)
    assert settings["trigger_mode"] == "trigger"
    assert settings["trigger_word"] == "platform"
    assert settings["enabled"] is True


def test_save_writes_the_platform_namespace_and_keeps_unrelated_keys(home_env):
    (home_env / "config.yaml").write_text(
        "telegram:\n"
        "  reactions: true\n"
        "model: hermes\n",
        encoding="utf-8",
    )
    saved = save_business_settings(
        home_env, {"trigger_mode": "trigger", "trigger_word": "hello"})
    assert saved["trigger_mode"] == "trigger"
    assert saved["trigger_word"] == "hello"
    assert saved["enabled"] is True
    raw = read_user_config_raw(home_env / "config.yaml")
    assert raw["platforms"]["telegram"]["business"]["trigger_word"] == "hello"
    assert raw["telegram"]["reactions"] is True
    assert raw["model"] == "hermes"
    assert load_business_settings(home_env)["trigger_word"] == "hello"


def test_save_rejects_unknown_keys_and_bad_values(home_env):
    with pytest.raises(ValueError):
        save_business_settings(home_env, {"no_such_key": True})
    with pytest.raises(ValueError):
        # draft_ttl_hours is gone in v2 (no drafts); it is an unknown key now.
        save_business_settings(home_env, {"draft_ttl_hours": 24})
    with pytest.raises(ValueError):
        save_business_settings(home_env, {"trigger_mode": "shout"})
    with pytest.raises(ValueError):
        save_business_settings(home_env, {"trigger_word": "two words"})
    with pytest.raises(ValueError):
        save_business_settings(home_env, {"debounce_seconds": -1})
    with pytest.raises(ValueError):
        save_business_settings(home_env, {"enabled": "yes"})
    assert load_business_settings(home_env) == BUSINESS_DEFAULTS


def test_load_falls_back_to_defaults_for_bad_stored_values(home_env):
    (home_env / "config.yaml").write_text(
        "platforms:\n"
        "  telegram:\n"
        "    business:\n"
        "      trigger_mode: shout\n"
        "      debounce_seconds: -5\n"
        "      enabled: maybe\n",
        encoding="utf-8",
    )
    settings = load_business_settings(home_env)
    assert settings["trigger_mode"] == "direct"
    assert settings["debounce_seconds"] == 8
    assert settings["enabled"] is True
