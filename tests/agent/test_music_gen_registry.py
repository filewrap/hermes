"""Tests for agent/music_gen_registry.py — provider registration & active lookup."""

from __future__ import annotations

import pytest

from agent import music_gen_registry
from agent.music_gen_provider import (
    MusicGenProvider,
    error_response,
    save_b64_music,
    save_bytes_music,
    success_response,
)


class _FakeProvider(MusicGenProvider):
    def __init__(self, name: str, available: bool = True):
        self._name = name
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def generate(self, prompt, **kw):
        return success_response(
            audio=f"{self._name}://{prompt}", model="fake", prompt=prompt, provider=self._name
        )


@pytest.fixture(autouse=True)
def _reset_registry():
    music_gen_registry._reset_for_tests()
    yield
    music_gen_registry._reset_for_tests()


class TestRegisterProvider:

    def test_rejects_empty_name(self):
        class Empty(MusicGenProvider):
            @property
            def name(self) -> str:
                return ""

            def generate(self, prompt, **kw):
                return {}

        with pytest.raises(ValueError):
            music_gen_registry.register_provider(Empty())

    def test_rejects_non_provider(self):
        with pytest.raises(TypeError):
            music_gen_registry.register_provider(object())

    def test_list_is_sorted(self):
        music_gen_registry.register_provider(_FakeProvider("zeta"))
        music_gen_registry.register_provider(_FakeProvider("alpha"))
        names = [p.name for p in music_gen_registry.list_providers()]
        assert names == ["alpha", "zeta"]


class TestGetActiveProvider:

    def test_single_available_among_many_autoresolves(self, tmp_path, monkeypatch):
        """When several providers are registered but only one has credentials
        (is_available()), that one is auto-selected without config. Mirrors
        agent/video_gen_registry's availability-filtered fallback."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        music_gen_registry.register_provider(_FakeProvider("fal", available=False))
        music_gen_registry.register_provider(_FakeProvider("xai", available=False))
        music_gen_registry.register_provider(_FakeProvider("deepinfra", available=True))
        active = music_gen_registry.get_active_provider()
        assert active is not None and active.name == "deepinfra"

    def test_unknown_explicit_config_fails_closed(self, tmp_path, monkeypatch):
        """A typo must not silently route a paid request to another backend."""
        import hermes_yaml as yaml

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"music_gen": {"provider": "ghost"}})
        )
        music_gen_registry.register_provider(_FakeProvider("only"))
        assert music_gen_registry.get_active_provider() is None

    def test_configured_provider_returned_even_when_unavailable(self, tmp_path, monkeypatch):
        """An explicitly configured provider is returned even with no key, so the
        dispatcher can surface a precise missing-credentials error instead of
        silently switching backends. Mirrors video_gen_registry."""
        import hermes_yaml as yaml

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"music_gen": {"provider": "only"}})
        )
        music_gen_registry.register_provider(_FakeProvider("only", available=False))
        active = music_gen_registry.get_active_provider()
        assert active is not None and active.name == "only"


class TestResponseShapes:

    def test_success_response_carries_audio_contract(self):
        payload = success_response(
            audio="/tmp/song.mp3", model="fast", prompt="lofi loop",
            duration=30, format="mp3", provider="fake", lyrics="la la",
        )
        assert payload == {
            "success": True, "audio": "/tmp/song.mp3", "model": "fast",
            "prompt": "lofi loop", "duration": 30, "format": "mp3",
            "provider": "fake", "lyrics": "la la",
        }

    def test_error_response_carries_error_contract(self):
        payload = error_response(
            error="no key", error_type="missing_credentials",
            provider="fake", model="fast", prompt="lofi loop",
        )
        assert payload["success"] is False
        assert payload["audio"] is None
        assert payload["error_type"] == "missing_credentials"

    def test_default_capabilities_fail_closed(self):
        caps = _FakeProvider("fake").capabilities()
        assert caps["modalities"] == ["text"]
        assert caps["supports_lyrics"] is False
        assert caps["supports_instrumental"] is False
        assert caps["max_reference_images"] == 0


class TestSaveHelpers:

    def test_save_bytes_music_lands_under_generated_music(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        path = save_bytes_music(b"ID3fake", prefix="test", extension="mp3")
        assert path.parent.name == "tool-music-generation"
        assert path.parent.parent.name == "generated"
        assert path.read_bytes() == b"ID3fake"

    def test_save_b64_music_decodes(self, tmp_path, monkeypatch):
        import base64

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        path = save_b64_music(base64.b64encode(b"ID3fake").decode(), prefix="test")
        assert path.read_bytes() == b"ID3fake"
