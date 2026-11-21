"""Discovery smoke check: the five shipped music-gen plugins register through
the real plugin discovery path (plugins/AGENTS.md: load through real discovery
with a temp HERMES_HOME; assert behaviour, not counts)."""

from __future__ import annotations

import pytest

from agent import music_gen_registry


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch, tmp_path):
    music_gen_registry._reset_for_tests()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield
    music_gen_registry._reset_for_tests()


def test_all_five_music_gen_plugins_discover():
    import model_tools  # import triggers discover_plugins()

    from hermes_cli.plugins import discover_plugins

    discover_plugins(force=True)
    names = {p.name for p in music_gen_registry.list_providers()}
    assert {"fal", "minimax", "google", "openrouter", "comfy"} <= names


def test_no_active_provider_without_configuration():
    import model_tools  # import triggers discover_plugins()

    from hermes_cli.plugins import discover_plugins

    discover_plugins(force=True)
    # Five backends are available but none is configured, so nothing is active
    # and the tool fails closed instead of guessing.
    assert music_gen_registry.get_active_provider() is None
