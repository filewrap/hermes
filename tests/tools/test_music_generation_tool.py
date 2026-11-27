"""Tests for the ``music_generate`` tool — background lifecycle, duplicate guard,
provider fallback, and capability-aware normalisation.

The handler runs through the real tool registry (``registry.dispatch``); providers
are in-process fakes writing into the temp ``HERMES_HOME``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from agent import music_gen_registry
from agent.music_gen_provider import (
    MusicGenProvider,
    error_response,
    save_bytes_music,
    success_response,
)
from tests.waiting import wait_until
from tools import music_generation_tool  # import registers the tool
from tools.registry import registry


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch, tmp_path):
    music_gen_registry._reset_for_tests()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.plugins as plugins_module

    monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda **kwargs: None)
    yield
    music_gen_registry._reset_for_tests()


class _FakeProvider(MusicGenProvider):
    """Writes fixed bytes and records the kwargs the tool layer hands it."""

    def __init__(self, name: str = "fake", *, supports_lyrics: bool = True,
                 supports_instrumental: bool = True, fail: bool = False):
        self._name = name
        self._supports_lyrics = supports_lyrics
        self._supports_instrumental = supports_instrumental
        self._fail = fail
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    def list_models(self) -> list[dict[str, Any]]:
        return [{"id": f"{self._name}-model", "display": self._name,
                 "supports_lyrics": self._supports_lyrics,
                 "supports_instrumental": self._supports_instrumental}]

    def default_model(self) -> Optional[str]:
        return f"{self._name}-model"

    def capabilities(self) -> dict[str, Any]:
        return {"modalities": ["text"], "formats": ["mp3", "wav"],
                "max_duration": 120, "min_duration": 1,
                "supports_lyrics": self._supports_lyrics,
                "supports_instrumental": self._supports_instrumental,
                "supports_duration": True, "supports_format": True,
                "supports_seed": False, "max_reference_images": 0}

    def generate(self, prompt, **kwargs):
        self.calls += 1
        self.last_kwargs = {"prompt": prompt, **kwargs}
        if self._fail:
            return error_response(error="fake failure", error_type="api_error",
                                  provider=self._name, prompt=prompt)
        path = save_bytes_music(b"FAKEAUDIO", prefix=self._name, extension="mp3")
        return success_response(audio=str(path), model=kwargs.get("model") or f"{self._name}-model",
                                prompt=prompt, duration=30, format="mp3", provider=self._name)


def _call(args: dict[str, Any]) -> dict[str, Any]:
    raw = registry.dispatch("music_generate", args)
    return json.loads(raw) if isinstance(raw, str) else raw


def _wait_terminal(task_id: str, home) -> dict[str, Any]:
    from tools import music_generation_tasks as tasks

    def _terminal():
        record = tasks.get_task(home, task_id)
        return record if record and record["status"] in ("succeeded", "failed") else None

    return wait_until(_terminal, timeout=10, what="music task to finish")


class TestGenerateLifecycle:
    def test_generate_runs_in_background_and_succeeds(self, tmp_path):
        music_gen_registry.register_provider(_FakeProvider("fake"))
        started = _call({"prompt": "lofi loop"})
        assert started["success"] is True
        assert started["status"] == "pending"
        task_id = started["task_id"]
        record = _wait_terminal(task_id, tmp_path)
        assert record["status"] == "succeeded"
        assert record["audio"] is not None
        from pathlib import Path
        assert Path(record["audio"]).read_bytes() == b"FAKEAUDIO"
        status = _call({"action": "status", "task_id": task_id})
        assert status["task"]["status"] == "succeeded"
        assert status["task"]["audio"] == record["audio"]

    def test_duplicate_guard_returns_existing_task(self, tmp_path):
        music_gen_registry.register_provider(_FakeProvider("fake"))
        first = _call({"prompt": "lofi loop"})
        second = _call({"prompt": "lofi loop"})
        assert second["task_id"] == first["task_id"]
        assert second["duplicate"] is True
        record = _wait_terminal(first["task_id"], tmp_path)
        assert record["status"] == "succeeded"
        third = _call({"prompt": "lofi loop"})
        assert third["task_id"] == first["task_id"]
        assert third["duplicate"] is True
        assert third["status"] == "succeeded"
        assert third["audio"] == record["audio"]

    def test_list_shows_providers_and_tasks(self, tmp_path):
        music_gen_registry.register_provider(_FakeProvider("fake"))
        started = _call({"prompt": "lofi loop"})
        listed = _call({"action": "list"})
        assert listed["success"] is True
        assert any(p["id"] == "fake" for p in listed["providers"])
        assert any(t["task_id"] == started["task_id"] for t in listed["tasks"])

    def test_status_unknown_task_errors(self):
        music_gen_registry.register_provider(_FakeProvider("fake"))
        result = _call({"action": "status", "task_id": "nope"})
        assert result["success"] is False

    def test_status_without_id_returns_recent_task(self, tmp_path):
        music_gen_registry.register_provider(_FakeProvider("fake"))
        started = _call({"prompt": "lofi loop"})
        status = _call({"action": "status"})
        assert status["task"]["id"] == started["task_id"]

    def test_missing_prompt_errors(self):
        music_gen_registry.register_provider(_FakeProvider("fake"))
        assert _call({"prompt": "  "})["success"] is False

    def test_bad_format_errors(self):
        music_gen_registry.register_provider(_FakeProvider("fake"))
        assert _call({"prompt": "x", "format": "ogg"})["success"] is False

    def test_no_provider_configured(self):
        result = _call({"prompt": "x"})
        assert result["success"] is False
        assert result["error_type"] == "no_provider_configured"


class TestFallbackAndNormalisation:
    def test_fallback_second_provider_succeeds(self, tmp_path):
        music_gen_registry.register_provider(_FakeProvider("aaa", fail=True))
        music_gen_registry.register_provider(_FakeProvider("zzz"))
        started = _call({"prompt": "lofi loop"})
        record = _wait_terminal(started["task_id"], tmp_path)
        assert record["status"] == "succeeded"
        assert record["provider"] == "zzz"
        assert [a["provider"] for a in record["attempts"]] == ["aaa", "zzz"]
        assert record["attempts"][0]["error"]

    def test_capability_normalisation_drops_unsupported_lyrics(self, tmp_path):
        provider = _FakeProvider("fake", supports_lyrics=False)
        music_gen_registry.register_provider(provider)
        started = _call({"prompt": "a song", "lyrics": "la la la"})
        record = _wait_terminal(started["task_id"], tmp_path)
        assert record["status"] == "succeeded"
        assert "lyrics" not in provider.last_kwargs
        assert {"key": "lyrics", "value": "la la la"} in record["ignored_overrides"]

    def test_model_override_pins_provider(self, tmp_path):
        first = _FakeProvider("aaa")
        second = _FakeProvider("zzz")
        music_gen_registry.register_provider(first)
        music_gen_registry.register_provider(second)
        started = _call({"prompt": "lofi loop", "model": "zzz/zzz-model"})
        record = _wait_terminal(started["task_id"], tmp_path)
        assert record["status"] == "succeeded"
        assert record["provider"] == "zzz"
        assert first.calls == 0
        assert second.calls == 1

    def test_unknown_pinned_provider_errors(self):
        music_gen_registry.register_provider(_FakeProvider("fake"))
        result = _call({"prompt": "x", "model": "ghost/ghost-model"})
        assert result["success"] is False
        assert result["error_type"] == "provider_not_registered"


class TestDynamicSchema:
    def test_lyrics_advertised_only_when_supported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(music_generation_tool, "_read_configured_music_model", lambda: None)
        music_gen_registry.register_provider(_FakeProvider("with-lyrics", supports_lyrics=True))
        schema = music_generation_tool._build_dynamic_music_schema()
        assert "lyrics" in schema["parameters"]["properties"]
        assert "instrumental" in schema["parameters"]["properties"]

        music_gen_registry._reset_for_tests()
        music_gen_registry.register_provider(_FakeProvider("no-lyrics", supports_lyrics=False,
                                                          supports_instrumental=False))
        schema = music_generation_tool._build_dynamic_music_schema()
        assert "lyrics" not in schema["parameters"]["properties"]
        assert "instrumental" not in schema["parameters"]["properties"]

    def test_no_provider_schema_is_prompt_only(self, monkeypatch):
        monkeypatch.setattr(music_generation_tool, "_read_configured_music_model", lambda: None)
        schema = music_generation_tool._build_dynamic_music_schema()
        assert set(schema["parameters"]["properties"]) == {"action", "task_id", "prompt"}
