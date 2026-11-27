"""Acknowledgement fast path (beta): settings read/write round-trip.

The beta toggle persists ``platforms.telegram.ack_fast_path`` in the
profile's config.yaml (default off) via the atomic config writer, exactly
like the threaded-replies toggle. A missing or unreadable config reads as
off — the fast path never fires on a config it cannot read.
"""

from __future__ import annotations

from pathlib import Path

from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI


def _home_with_config(tmp_path, content: str) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(content, encoding="utf-8")
    return home


def test_ack_fast_path_setting_round_trip(tmp_path):
    home = _home_with_config(tmp_path, "platforms:\n  telegram: {}\n")
    assert TelegramPersonalSettingsUI._ack_fast_path_enabled(str(home)) is False
    TelegramPersonalSettingsUI._set_ack_fast_path(str(home), True)
    assert TelegramPersonalSettingsUI._ack_fast_path_enabled(str(home)) is True
    TelegramPersonalSettingsUI._set_ack_fast_path(str(home), False)
    assert TelegramPersonalSettingsUI._ack_fast_path_enabled(str(home)) is False


def test_ack_fast_path_setting_missing_config_reads_off(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    assert TelegramPersonalSettingsUI._ack_fast_path_enabled(str(home)) is False
    assert TelegramPersonalSettingsUI._ack_fast_path_enabled(str(tmp_path / "no-such-home")) is False
