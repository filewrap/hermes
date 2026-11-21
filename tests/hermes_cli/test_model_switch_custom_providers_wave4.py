"""Wave 4 models port: /models probe write-back preserves per-model rows.

A refresh of a DISCOVERED catalog adds/removes ids but never wipes
per-model row metadata (compat / cost / context_length) already on a
surviving row.
"""

from hermes_cli.model_switch_providers import _save_discovered_models_to_config


def test_save_discovered_models_preserves_row_metadata_on_refresh(monkeypatch):
    """A refresh of a DISCOVERED catalog adds/removes ids but never wipes
    per-model row metadata (compat / cost / context_length) already on a
    surviving row (wave 4 models port)."""
    save_calls = []

    def fake_save(config):
        save_calls.append(dict(config))

    monkeypatch.setattr("hermes_cli.config.save_config", fake_save)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "custom_providers": [
                {
                    "name": "my-gateway",
                    "base_url": "https://gateway.example.com/v1",
                    "models_discovered": True,
                    "models": {
                        "kept-model": {
                            "context_length": 8192,
                            "compat": {"supportsTools": True},
                            "cost": {"input": 1.0, "output": 2.0},
                        },
                        "dropped-model": {},
                    },
                }
            ]
        },
    )

    _save_discovered_models_to_config(
        "https://gateway.example.com/v1",
        ["kept-model", "new-model"],
    )
    assert len(save_calls) == 1
    saved = save_calls[0]["custom_providers"][0]
    assert saved["models_discovered"] is True
    assert saved["models"]["kept-model"] == {
        "context_length": 8192,
        "compat": {"supportsTools": True},
        "cost": {"input": 1.0, "output": 2.0},
    }
    assert saved["models"]["new-model"] == {}
    assert "dropped-model" not in saved["models"]
