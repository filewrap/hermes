"""``browser.profile_persistence``: "save" (default) runs local Chromium on one persistent profile
per active profile home (``<home>/browser/``); "auto" is the throwaway lane (a fresh profile per
session, use and throw). An explicit ``browser.user_data_dir`` always wins over the mode; a
persistent dir that cannot be created fails open to a throwaway session with a warning.
"""

import textwrap

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    for name in ("BROWSER_USE_API_KEY", "BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    _reset()
    yield home, managed
    _reset()


def _reset():
    import hermes_cli.config as cfg
    from hermes_cli import config_effective, managed_scope
    from tools import browser_session_cloud

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()
    browser_session_cloud.reset_caches()


def _write(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    _reset()


def test_default_mode_save_resolves_to_persistent_per_home_dir(homes):
    """No browser keys at all: the default is save, so the resolution is the persistent per-home
    dir and the dir is created on demand."""
    from tools.browser_session_cloud import profile_persistence, user_data_dir

    home, _managed = homes
    assert profile_persistence() == "save"
    assert user_data_dir() == str(home / "browser")
    assert (home / "browser").is_dir()


def test_mode_auto_resolves_to_throwaway(homes):
    """auto is the throwaway lane: no persistent dir, exactly the old empty user_data_dir answer."""
    from tools.browser_session_cloud import profile_persistence, user_data_dir

    home, _managed = homes
    _write(home / "config.yaml", """
        browser:
          profile_persistence: auto
        """)
    assert profile_persistence() == "auto"
    assert user_data_dir() == ""
    assert not (home / "browser").exists()


def test_explicit_user_data_dir_wins_over_save_mode(homes, tmp_path):
    from tools.browser_session_cloud import profile_persistence, user_data_dir

    home, _managed = homes
    _write(home / "config.yaml", f"""
        browser:
          profile_persistence: save
          user_data_dir: {tmp_path / "pinned"}
        """)
    assert profile_persistence() == "save"
    assert user_data_dir() == str(tmp_path / "pinned")


def test_explicit_user_data_dir_wins_over_auto_mode(homes, tmp_path):
    from tools.browser_session_cloud import profile_persistence, user_data_dir

    home, _managed = homes
    _write(home / "config.yaml", f"""
        browser:
          profile_persistence: auto
          user_data_dir: {tmp_path / "pinned"}
        """)
    assert profile_persistence() == "auto"
    assert user_data_dir() == str(tmp_path / "pinned")


def test_unknown_mode_falls_back_to_save(homes):
    """A misspelled mode never silently discards state: it warns and behaves as save."""
    from tools.browser_session_cloud import profile_persistence, user_data_dir

    home, _managed = homes
    _write(home / "config.yaml", """
        browser:
          profile_persistence: bogus
        """)
    assert profile_persistence() == "save"
    assert user_data_dir() == str(home / "browser")


def test_uncreatable_persistent_dir_fails_open_to_throwaway(homes, caplog):
    """A file squatting on the persistent dir path must not fail the turn: the resolution warns
    and falls back to a throwaway session for that turn."""
    import logging

    from tools.browser_session_cloud import user_data_dir

    home, _managed = homes
    (home / "browser").write_text("not a directory", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="tools.browser_session_cloud"):
        assert user_data_dir() == ""
    assert "persistent browser profile directory" in caplog.text


def test_two_profiles_get_separate_persistent_dirs(homes, tmp_path, monkeypatch):
    """Per-profile separation through the real home resolution: each home's save-mode dir is its
    own, so one profile's cookies/logins never land in another's jar."""
    from tools.browser_session_cloud import user_data_dir

    home_a, _managed = homes
    assert user_data_dir() == str(home_a / "browser")
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    _reset()
    assert user_data_dir() == str(home_b / "browser")
    assert (home_a / "browser").is_dir()
    assert (home_b / "browser").is_dir()
