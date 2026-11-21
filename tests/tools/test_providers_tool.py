"""Tests for the ``providers`` tool — management of Hermes model providers.

The handler runs through the real tool registry (``registry.dispatch``) against
a temp ``HERMES_HOME``: real config reads/writes, real credential-pool
writes, real backup zips. Most probes are stubbed at
``hermes_cli.models.probe_api_models``; two tests run the real probe against
a loopback ``/models`` server — no external network, no real keys anywhere
(the dummy key below is a test-only string, never a real credential).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import tools.providers_tool  # import registers the tool
from tools.registry import registry

DUMMY_KEY = "sk-test-dummy-key-0001"


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {}}), encoding="utf-8")
    (hermes_home / ".env").write_text("", encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"provider": "openrouter", "default": "test-model"}}),
        encoding="utf-8")
    import hermes_cli.plugins as plugins_module

    monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda **kwargs: None)
    return hermes_home


def _call(args):
    raw = registry.dispatch("providers", args)
    assert isinstance(raw, str)
    return json.loads(raw)


def _read_config(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}


def _write_config(home: Path, cfg: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _row(result, slug):
    rows = [row for row in result["providers"] if row["slug"] == slug]
    assert len(rows) == 1, f"expected one row for {slug}, got {[r['slug'] for r in result['providers']]}"
    return rows[0]


def test_tool_registered_with_providers_toolset():
    entry = registry.get_entry("providers")
    assert entry is not None
    assert entry.toolset == "providers"
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    assert "providers" in TOOLSETS["providers"]["tools"]
    assert "providers" in _HERMES_CORE_TOOLS
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS

    assert any(key == "providers" for key, _label, _desc in CONFIGURABLE_TOOLSETS)
    assert set(tools.providers_tool.PROVIDERS_ACTIONS) <= set(
        tools.providers_tool.PROVIDERS_SCHEMA["parameters"]["properties"]["action"]["enum"])


def test_list_empty_home_shows_canonical_rows_and_no_key_values(_home):
    result = _call({"action": "list"})
    assert result["ok"] is True
    assert result["current"]["provider"] == "openrouter"
    row = _row(result, "openrouter")
    assert row["is_user_defined"] is False
    assert row["credential_count"] == 0
    assert DUMMY_KEY not in json.dumps(result)


def test_add_custom_endpoint_writes_key_to_env_not_config(_home):
    result = _call({
        "action": "add", "name": "Honey Qwen",
        "base_url": "https://honey.example/v1", "model": "Qwen3.8-27B-Uncensored",
        "api_key": DUMMY_KEY,
    })
    assert result["ok"] is True, result
    assert result["created"] is True
    assert result["provider"] == "honey-qwen"
    assert DUMMY_KEY not in json.dumps(result)
    saved = _read_config(_home)
    entry = saved["providers"]["honey-qwen"]
    assert entry["base_url"] == "https://honey.example/v1"
    assert entry["model"] == "Qwen3.8-27B-Uncensored"
    assert "api_key" not in entry
    assert entry["key_env"] == "HERMES_CUSTOM_HONEY_QWEN_API_KEY"
    assert DUMMY_KEY in (_home / ".env").read_text(encoding="utf-8")
    assert DUMMY_KEY not in (_home / "config.yaml").read_text(encoding="utf-8")

    listed = _call({"action": "list"})
    row = _row(listed, "honey-qwen")
    assert row["is_user_defined"] is True
    assert row["default_model"] == "Qwen3.8-27B-Uncensored"
    assert DUMMY_KEY not in json.dumps(listed)


def test_add_key_to_canonical_provider(_home):
    result = _call({"action": "add", "provider": "openrouter", "api_key": DUMMY_KEY, "label": "main"})
    assert result["ok"] is True, result
    assert result["credential_count"] == 1
    assert DUMMY_KEY not in json.dumps(result)
    listed = _call({"action": "list"})
    assert _row(listed, "openrouter")["credential_count"] == 1
    assert _row(listed, "openrouter")["configured"] is True


def test_add_custom_endpoint_needs_name_url_model(_home):
    assert _call({"action": "add", "name": "No URL", "model": "m"})["ok"] is False
    assert _call({"action": "add", "provider": "openrouter"})["ok"] is False


@pytest.mark.parametrize("entry", [
    # Disabled: the compatible view drops it, but the UI still renders the row.
    {"name": "honey qwen", "base_url": "https://honey.example/v1",
     "model": "Qwen3.8-27B-Uncensored", "enabled": False},
    # No base_url: likewise dropped from the compatible view.
    {"name": "honey qwen", "model": "Qwen3.8-27B-Uncensored"},
    # Nameless: the row's name IS the stored key, so the delete cannot fall
    # back on a name match — the key itself must resolve, prefix and all.
    {"base_url": "https://honey.example/v1",
     "model": "Qwen3.8-27B-Uncensored", "enabled": False},
])
def test_remove_deletes_view_dropped_entries(_home, entry):
    """A custom provider the compatible view drops still renders as a UI row,
    so its delete must resolve it straight from config — including a
    mixed-case stored key the UI slug lowercases."""
    _write_config(_home, {"providers": {"Custom:Honey-Qwen": entry}})

    result = _call({"action": "remove", "provider": "custom:honey-qwen"})
    assert result["ok"] is True, result
    assert result["removed_config"] is True
    assert _read_config(_home).get("providers") in (None, {})
    assert _call({"action": "remove", "provider": "custom:honey-qwen"})["ok"] is False


def test_remove_deletes_dual_legacy_and_keyed_entries(_home):
    """One provider stored twice — a legacy custom_providers entry and a keyed
    providers entry — renders two UI rows for one endpoint, so one delete
    must remove both, or the provider comes back after the delete."""
    _write_config(_home, {
        "custom_providers": [{
            "name": "honey qwen", "base_url": "https://honey.example/v1",
            "model": "Qwen3.8-27B-Uncensored",
        }],
        "providers": {"Custom:Honey-Qwen": {
            "name": "honey qwen", "base_url": "https://honey.example/v1",
            "model": "Qwen3.8-27B-Uncensored", "enabled": False,
        }},
    })

    result = _call({"action": "remove", "provider": "custom:honey-qwen"})
    assert result["ok"] is True, result
    assert result["removed_config"] is True
    saved = _read_config(_home)
    assert saved.get("custom_providers") in (None, [])
    assert saved.get("providers") in (None, {})
    assert _call({"action": "remove", "provider": "custom:honey-qwen"})["ok"] is False


def test_remove_refuses_canonical_and_unknown(_home):
    assert _call({"action": "remove", "provider": "openrouter"})["ok"] is False
    result = _call({"action": "remove", "provider": "nosuch-provider"})
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_remove_detaches_live_route(_home):
    _write_config(_home, {
        "model": {"provider": "custom:honey-qwen", "default": "Qwen3.8-27B-Uncensored",
                  "base_url": "https://honey.example/v1"},
        "providers": {"honey-qwen": {
            "name": "honey qwen", "base_url": "https://honey.example/v1",
            "model": "Qwen3.8-27B-Uncensored",
        }},
    })
    result = _call({"action": "remove", "provider": "honey-qwen"})
    assert result["ok"] is True, result
    assert result["route_detached"] is True
    assert "provider" not in _read_config(_home).get("model", {})


def test_remove_credential_by_label(_home):
    _call({"action": "add_key", "provider": "openrouter", "api_key": DUMMY_KEY, "label": "one"})
    _call({"action": "add_key", "provider": "openrouter", "api_key": "sk-test-dummy-key-0002", "label": "two"})
    result = _call({"action": "remove_credential", "provider": "openrouter", "target": "one"})
    assert result["ok"] is True, result
    assert result["remaining"] == 1
    assert DUMMY_KEY not in json.dumps(result)
    assert _call({"action": "remove_credential", "provider": "openrouter", "target": "one"})["ok"] is False


def test_update_resaves_endpoint_without_forking(_home):
    _call({"action": "add", "name": "Honey Qwen",
           "base_url": "https://honey.example/v1", "model": "Qwen3.8-27B-Uncensored"})
    result = _call({"action": "update", "provider": "honey-qwen", "name": "Honey Qwen",
                    "base_url": "https://honey.example/v2", "model": "Qwen3-32B"})
    assert result["ok"] is True, result
    assert result["created"] is False
    saved = _read_config(_home)
    assert list(saved["providers"]) == ["honey-qwen"]
    assert saved["providers"]["honey-qwen"]["base_url"] == "https://honey.example/v2"
    assert saved["providers"]["honey-qwen"]["model"] == "Qwen3-32B"
    assert _call({"action": "update", "provider": "nosuch", "name": "No Such",
                  "base_url": "https://x.example/v1", "model": "m"})["ok"] is False


def test_modify_default_model_updates_live_route(_home):
    _write_config(_home, {
        "model": {"provider": "custom:honey-qwen", "default": "old-model",
                  "base_url": "https://honey.example/v1"},
        "providers": {"honey-qwen": {
            "name": "honey qwen", "base_url": "https://honey.example/v1",
            "model": "old-model", "default_model": "old-model",
        }},
    })
    result = _call({"action": "modify", "provider": "honey-qwen",
                    "default_model": "new-model", "enabled": False})
    assert result["ok"] is True, result
    assert result["route_updated"] is True
    saved = _read_config(_home)
    entry = saved["providers"]["honey-qwen"]
    assert entry["default_model"] == "new-model"
    assert entry["model"] == "new-model"
    assert entry["enabled"] is False
    assert saved["model"]["default"] == "new-model"
    assert _call({"action": "modify", "provider": "openrouter", "enabled": False})["ok"] is False


def test_set_default_model_custom_entry(_home):
    _write_config(_home, {
        "providers": {"honey-qwen": {
            "name": "honey qwen", "base_url": "https://honey.example/v1",
            "model": "old-model",
        }},
    })
    result = _call({"action": "set_default_model", "provider": "honey-qwen", "model": "new-model"})
    assert result["ok"] is True, result
    assert result["route_updated"] is False
    saved = _read_config(_home)
    assert saved["providers"]["honey-qwen"]["default_model"] == "new-model"
    assert saved["providers"]["honey-qwen"]["model"] == "new-model"
    assert _call({"action": "set_default_model", "provider": "nosuch", "model": "m"})["ok"] is False


def test_move_reorders_keyed_providers(_home):
    _write_config(_home, {"providers": {
        "a-one": {"name": "a one", "base_url": "https://a.example/v1", "model": "m"},
        "b-two": {"name": "b two", "base_url": "https://b.example/v1", "model": "m"},
        "c-three": {"name": "c three", "base_url": "https://c.example/v1", "model": "m"},
    }})
    result = _call({"action": "move", "provider": "c-three", "position": 1})
    assert result["ok"] is True, result
    assert result["position"] == 1
    assert result["order"] == ["c-three", "a-one", "b-two"]
    assert list(_read_config(_home)["providers"]) == ["c-three", "a-one", "b-two"]
    assert _call({"action": "move", "provider": "openrouter", "position": 1})["ok"] is False


def test_bulk_remove_reports_per_provider(_home):
    _write_config(_home, {"providers": {
        "a-one": {"name": "a one", "base_url": "https://a.example/v1", "model": "m"},
        "b-two": {"name": "b two", "base_url": "https://b.example/v1", "model": "m"},
    }})
    result = _call({"action": "bulk_remove", "providers": ["a-one", "nosuch", "b-two"]})
    assert result["ok"] is True, result
    assert result["removed"] == 2
    assert result["attempted"] == 3
    assert [item["removed"] for item in result["results"]] == [True, False, True]
    assert _read_config(_home).get("providers") in (None, {})
    assert _call({"action": "bulk_remove", "providers": []})["ok"] is False


def test_before_add_probes_without_persisting(_home, monkeypatch):
    seen = {}

    def _fake_probe(api_key, base_url, timeout=5.0, api_mode=None, request_headers=None):
        seen["base_url"] = base_url
        seen["api_key"] = api_key
        return {"models": ["probe-model-1", "probe-model-2"], "probed_url": base_url + "/models",
                "resolved_base_url": base_url, "suggested_base_url": None, "used_fallback": False}

    monkeypatch.setattr("hermes_cli.models.probe_api_models", _fake_probe)
    result = _call({"action": "test", "base_url": "https://probe.example/v1", "api_key": DUMMY_KEY})
    assert result["ok"] is True, result
    assert result["persisted"] is False
    assert result["total_models"] == 2
    assert result["models"] == ["probe-model-1", "probe-model-2"]
    assert seen == {"base_url": "https://probe.example/v1", "api_key": DUMMY_KEY}
    assert DUMMY_KEY not in json.dumps(result)
    assert "providers" not in _read_config(_home)


class _ModelsHandler:
    """Minimal OpenAI-compatible /models endpoint on loopback (live probe, no stub)."""

    def __init__(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/models":
                    body = json.dumps({"data": [{"id": "dummy-live-1"}, {"id": "dummy-live-2"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


def test_live_probe_against_local_endpoint_end_to_end(_home):
    """Add/list/test/remove against a real loopback /models server: the probe
    runs the real ``probe_api_models`` path (no stub, no external network).

    A freshly added endpoint carries its hand-typed default model as a
    ``models`` mapping, which the write-back guard treats as user-curated —
    the same refusal the picker's write-back gives — so the probe reports
    but writes nothing back, and the entry keeps exactly what the user typed.
    """
    with _ModelsHandler() as base_url:
        added = _call({"action": "add", "name": "Local Dummy",
                       "base_url": base_url, "model": "dummy-live-1"})
        assert added["ok"] is True, added
        listed = _call({"action": "list"})
        assert _row(listed, "local-dummy")["default_model"] == "dummy-live-1"
        tested = _call({"action": "test", "provider": "local-dummy", "timeout": 10})
        assert tested["ok"] is True, tested
        assert tested["reachable"] is True
        assert tested["total_models"] == 2
        assert tested["models_written_back"] is False
        saved = _read_config(_home)
        assert saved["providers"]["local-dummy"]["models"] == {"dummy-live-1": {}}
        removed = _call({"action": "remove", "provider": "local-dummy"})
        assert removed["ok"] is True, removed
        assert _read_config(_home).get("providers") in (None, {})


def test_live_probe_writes_back_when_no_curated_models(_home):
    """Live probe write-back on an entry with no ``models`` mapping: the
    guard allows it, so the discovered ids land in the saved entry."""
    with _ModelsHandler() as base_url:
        _write_config(_home, {"providers": {"local-dummy": {
            "name": "Local Dummy", "base_url": base_url, "model": "dummy-live-1",
        }}})
        tested = _call({"action": "test", "provider": "local-dummy", "timeout": 10})
        assert tested["ok"] is True, tested
        assert tested["reachable"] is True
        assert tested["total_models"] == 2
        assert tested["models_written_back"] is True
        saved = _read_config(_home)
        assert sorted(saved["providers"]["local-dummy"]["models"]) == ["dummy-live-1", "dummy-live-2"]


def test_live_test_writes_back_discovered_models(_home, monkeypatch):
    _write_config(_home, {"providers": {"honey-qwen": {
        "name": "honey qwen", "base_url": "https://honey.example/v1", "model": "qwen-old",
    }}})

    def _fake_probe(api_key, base_url, timeout=5.0, api_mode=None, request_headers=None):
        return {"models": ["qwen-new-1", "qwen-new-2"], "probed_url": base_url + "/models",
                "resolved_base_url": base_url, "suggested_base_url": None, "used_fallback": False}

    monkeypatch.setattr("hermes_cli.models.probe_api_models", _fake_probe)
    result = _call({"action": "test", "provider": "honey-qwen"})
    assert result["ok"] is True, result
    assert result["models_written_back"] is True
    saved = _read_config(_home)
    entry = saved["providers"]["honey-qwen"]
    assert entry["models_discovered"] is True
    assert sorted(entry["models"]) == ["qwen-new-1", "qwen-new-2"]


def test_live_test_unreachable_reports_no_models(_home, monkeypatch):
    _write_config(_home, {"providers": {"honey-qwen": {
        "name": "honey qwen", "base_url": "https://down.example/v1", "model": "m",
    }}})

    def _fake_probe(api_key, base_url, timeout=5.0, api_mode=None, request_headers=None):
        return {"models": None, "probed_url": base_url + "/models",
                "resolved_base_url": base_url, "suggested_base_url": None, "used_fallback": False}

    monkeypatch.setattr("hermes_cli.models.probe_api_models", _fake_probe)
    result = _call({"action": "test", "provider": "honey-qwen"})
    assert result["ok"] is False
    assert result["reachable"] is False
    assert result["models_written_back"] is False


def test_backup_writes_zip_under_home(_home):
    result = _call({"action": "backup"})
    assert result["ok"] is True, result
    assert result["complete"] is True
    backup_path = Path(result["path"])
    assert backup_path.suffix == ".zip"
    assert backup_path.exists()
    assert backup_path.stat().st_size > 0
    assert str(_home) in str(backup_path)


def test_unknown_action_is_an_error():
    result = _call({"action": "explode"})
    assert result["ok"] is False
    assert "unknown action" in result["error"]
