"""Regression coverage for profile-local Telegram preferences during rollout."""
from __future__ import annotations

import json


def _force_local_even_when_old_rollout_flag_is_true(monkeypatch):
    from agent import storage_mode
    from gateway import mongo_multiuser

    monkeypatch.setattr(storage_mode, "mongo_primary_enabled", lambda home=None: False)
    monkeypatch.setattr(mongo_multiuser, "enabled", lambda home=None: True)
    # Any attempted lookup would prove a preference module still treats the old
    # rollout flag as a storage-backend selector.
    def unexpected_store(*args, **kwargs):
        raise AssertionError("tenant store must not be constructed for preferences")

    monkeypatch.setattr(mongo_multiuser, "store_for_home", unexpected_store)


def test_personal_json_preferences_remain_canonical_with_old_rollout_flag(tmp_path, monkeypatch):
    from gateway.telegram_personal_settings import (
        load_personal_settings,
        personal_settings_path,
        save_personal_settings,
    )

    _force_local_even_when_old_rollout_flag_is_true(monkeypatch)
    path = personal_settings_path(tmp_path, "101")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"general_instructions": "existing local value"}), encoding="utf-8")

    assert load_personal_settings(tmp_path, "101")["general_instructions"] == "existing local value"
    saved = save_personal_settings(tmp_path, "101", {"search_instructions": "updated locally"})
    assert saved["general_instructions"] == "existing local value"
    assert json.loads(path.read_text(encoding="utf-8"))["search_instructions"] == "updated locally"
    assert load_personal_settings(tmp_path, "102")["general_instructions"] == ""


def test_telegram_user_json_preferences_remain_canonical_and_isolated(tmp_path, monkeypatch):
    from plugins.platforms.telegram import telegram_user_settings_store as settings

    _force_local_even_when_old_rollout_flag_is_true(monkeypatch)
    settings.save_user_settings(tmp_path, "201", {"name": "Alice", "instructions": {"general": "keep this"}})
    settings.save_user_settings(tmp_path, "202", {"name": "Bob"})

    assert settings.load_user_settings(tmp_path, "201")["name"] == "Alice"
    assert settings.load_user_settings(tmp_path, "201")["instructions"]["general"] == "keep this"
    assert settings.load_user_settings(tmp_path, "202")["name"] == "Bob"
    assert settings.load_user_settings(tmp_path, "-201")["name"] == ""
    assert (tmp_path / "telegram_user_settings.sqlite3").is_file()


def test_preferences_do_not_bypass_strict_personal_scope(tmp_path):
    from gateway.telegram_personal_settings import personal_settings_path

    try:
        personal_settings_path(tmp_path, "../102")
    except ValueError:
        pass
    else:
        raise AssertionError("non-numeric user IDs must remain rejected")
