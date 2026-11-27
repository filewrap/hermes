"""Tests for Gemini setup: a free-tier key is accepted, like any other key.

Fork rule: no setup-time credential gate. Upstream refused to wire a free-tier
Gemini key during setup; the fork deletes that refusal, so the key is saved
and the provider proceeds exactly like a paid-tier key.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty config."""
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: some-old-model\n")
    (home / ".env").write_text("")
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Clear any ambient env that could alter provider resolution
    for var in (
        "HERMES_MODEL",
        "LLM_MODEL",
        "HERMES_INFERENCE_PROVIDER",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "GEMINI_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


class TestGeminiSetupFreeTierAccepted:
    """A free-tier Gemini key is wired up like any other key (fork: no setup gate)."""

    def test_free_tier_key_is_accepted(self, config_home, monkeypatch, capsys):
        """Free-tier probe result -> provider IS saved, no refusal is printed."""
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-free-tier-key")

        from hermes_cli.model_setup_flows import _model_flow_api_key_provider
        from hermes_cli.config import load_config

        # The tier probe is no longer consulted at setup; patch it to prove
        # the flow does not depend on it either way.
        with patch(
            "plugins.model_providers.google.native_adapter.probe_gemini_tier",
            return_value="free",
        ), patch(
            "hermes_cli.auth._prompt_model_selection",
            return_value="gemini-2.5-flash",
        ), patch(
            "hermes_cli.auth.deactivate_provider",
        ), patch("builtins.input", return_value=""):
            _model_flow_api_key_provider(load_config(), "gemini", "old-model")

        output = capsys.readouterr().out
        assert "Not saving Gemini" not in output

        # Config must show gemini as the provider
        import hermes_yaml as yaml
        cfg = yaml.safe_load((config_home / "config.yaml").read_text()) or {}
        model = cfg.get("model")
        assert isinstance(model, dict), f"model should be dict, got {type(model)}"
        assert model.get("provider") == "gemini"
        assert model.get("default") == "gemini-2.5-flash"

    def test_paid_tier_key_proceeds(self, config_home, monkeypatch, capsys):
        """Paid-tier probe result -> provider IS saved normally."""
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-paid-tier-key")

        from hermes_cli.model_setup_flows import _model_flow_api_key_provider
        from hermes_cli.config import load_config

        with patch(
            "plugins.model_providers.google.native_adapter.probe_gemini_tier",
            return_value="paid",
        ), patch(
            "hermes_cli.auth._prompt_model_selection",
            return_value="gemini-2.5-flash",
        ), patch(
            "hermes_cli.auth.deactivate_provider",
        ), patch("builtins.input", return_value=""):
            _model_flow_api_key_provider(load_config(), "gemini", "old-model")

        output = capsys.readouterr().out
        # No tier output at all: the gate (and its "Tier check: paid" print)
        # is deleted, so setup never consults the probe.
        assert "Tier check" not in output
        assert "Not saving Gemini" not in output

        import hermes_yaml as yaml
        cfg = yaml.safe_load((config_home / "config.yaml").read_text()) or {}
        model = cfg.get("model")
        assert isinstance(model, dict), f"model should be dict, got {type(model)}"
        assert model.get("provider") == "gemini"
        assert model.get("default") == "gemini-2.5-flash"


    def test_non_gemini_provider_skips_probe(self, config_home, monkeypatch):
        """Probe must only run for provider_id == 'gemini', not for other providers."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")

        from hermes_cli.model_setup_flows import _model_flow_api_key_provider
        from hermes_cli.config import load_config

        with patch(
            "plugins.model_providers.google.native_adapter.probe_gemini_tier",
        ) as mock_probe, patch(
            "hermes_cli.auth._prompt_model_selection",
            return_value="deepseek-chat",
        ), patch(
            "hermes_cli.auth.deactivate_provider",
        ), patch("builtins.input", return_value=""):
            _model_flow_api_key_provider(load_config(), "deepseek", "old-model")

        mock_probe.assert_not_called()
