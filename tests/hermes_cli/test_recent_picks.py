"""Behaviour contracts for per-profile recent providers/models."""

from __future__ import annotations

from pathlib import Path

import hermes_yaml as yaml
from hermes_cli.model_switch import ModelSwitchResult, persist_model_selection
from hermes_cli.recent_picks import (
    MAX_RECENT_PICKS,
    load_recent_picks,
    record_recent_route,
)


def _seed_config(path: Path) -> None:
    path.write_text("model:\n  provider: openrouter\n  name: vendor/model-a\n")


def test_record_prepends_dedupes_and_caps() -> None:
    config: dict = {}
    record_recent_route(config, "OpenRouter", "vendor/model-a")
    record_recent_route(config, "nous", "model-x")
    record_recent_route(config, "OpenRouter", "vendor/model-b")
    record_recent_route(config, "openrouter", "vendor/model-a")
    providers, models = load_recent_picks(config)
    assert providers == ["openrouter", "nous"]
    assert models == {
        "openrouter": ["vendor/model-a", "vendor/model-b"],
        "nous": ["model-x"],
    }
    for i in range(MAX_RECENT_PICKS + 2):
        record_recent_route(config, f"p{i}", "m")
    providers, _ = load_recent_picks(config)
    assert len(providers) == MAX_RECENT_PICKS
    assert providers[0] == f"p{MAX_RECENT_PICKS + 1}"


def test_record_ignores_empty_and_load_tolerates_malformed() -> None:
    config: dict = {}
    record_recent_route(config, "", "vendor/model-a")
    record_recent_route(config, "openrouter", "")
    assert config == {}
    providers, models = load_recent_picks(
        {"_recent_providers": "not-a-list", "_recent_models": {"openrouter": "nope"}}
    )
    assert providers == [] and models == {}


def test_persist_model_selection_records_the_route(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _seed_config(config_path)
    result = ModelSwitchResult(
        success=True, new_model="vendor/model-b", target_provider="openrouter",
        provider_changed=False,
    )
    persist_model_selection(result, config_path)
    config = yaml.safe_load(config_path.read_text())
    providers, models = load_recent_picks(config)
    assert providers == ["openrouter"]
    assert models == {"openrouter": ["vendor/model-b"]}
    result = ModelSwitchResult(
        success=True, new_model="model-x", target_provider="nous", provider_changed=True
    )
    persist_model_selection(result, config_path)
    config = yaml.safe_load(config_path.read_text())
    providers, models = load_recent_picks(config)
    assert providers == ["nous", "openrouter"]
    assert models["nous"] == ["model-x"]
    assert models["openrouter"] == ["vendor/model-b"]
