"""Tests for the FAL music gen plugin — payload shape, result parsing, dispatch."""

from __future__ import annotations

import pytest

from agent import music_gen_registry


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch, tmp_path):
    music_gen_registry._reset_for_tests()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield
    music_gen_registry._reset_for_tests()


def test_minimax_payload_shape():
    from plugins.music_gen.fal import _build_payload

    payload = _build_payload(model="fal-ai/minimax-music/v2.6", prompt="a song",
                             lyrics="la la", instrumental=None, duration=30, format="mp3")
    assert payload["lyrics"] == "la la"
    assert payload["duration"] == 30
    assert payload["audio_setting"] == {"sample_rate": 44100, "bitrate": 256000, "format": "mp3"}


def test_minimax_payload_lyrics_optimizer_when_no_lyrics():
    from plugins.music_gen.fal import _build_payload

    payload = _build_payload(model="fal-ai/minimax-music/v2.6", prompt="a song",
                             lyrics=None, instrumental=None, duration=None, format="mp3")
    assert payload["lyrics_optimizer"] is True
    assert "duration" not in payload


def test_ace_step_payload_has_no_lyrics_or_audio_setting():
    from plugins.music_gen.fal import _build_payload

    payload = _build_payload(model="fal-ai/ace-step/prompt-to-audio", prompt="a beat",
                             lyrics=None, instrumental=True, duration=60, format="wav")
    assert payload == {"prompt": "a beat", "instrumental": True, "duration": 60}


def test_stable_audio_payload_uses_seconds_total():
    from plugins.music_gen.fal import _build_payload

    payload = _build_payload(model="fal-ai/stable-audio-25/text-to-audio", prompt="rain",
                             lyrics=None, instrumental=None, duration=45, format="wav")
    assert payload == {"prompt": "rain", "seconds_total": 45}


def test_lyrics_plus_instrumental_rejected():
    from plugins.music_gen.fal import _build_payload

    with pytest.raises(ValueError, match="cannot use lyrics when instrumental"):
        _build_payload(model="fal-ai/minimax-music/v2.6", prompt="x", lyrics="la",
                       instrumental=True, duration=None, format="mp3")


def test_ace_step_lyrics_rejected():
    from plugins.music_gen.fal import _build_payload

    with pytest.raises(ValueError, match="does not support explicit lyrics"):
        _build_payload(model="fal-ai/ace-step/prompt-to-audio", prompt="x", lyrics="la",
                       instrumental=None, duration=None, format="wav")


def test_audio_url_extraction():
    from plugins.music_gen.fal import _audio_url_from_result

    assert _audio_url_from_result({"audio": {"url": "https://cdn/x.mp3"}}) == "https://cdn/x.mp3"
    assert _audio_url_from_result({"audio_url": "https://cdn/y.mp3"}) == "https://cdn/y.mp3"
    assert _audio_url_from_result({"audio": "https://cdn/z.mp3"}) == "https://cdn/z.mp3"
    assert _audio_url_from_result({"nothing": True}) is None
    assert _audio_url_from_result(None) is None


def test_catalog_declares_default_and_per_model_formats():
    from plugins.music_gen.fal import DEFAULT_MODEL, FALMusicGenProvider

    provider = FALMusicGenProvider()
    assert provider.default_model() == DEFAULT_MODEL
    models = {m["id"]: m for m in provider.list_models()}
    assert models[DEFAULT_MODEL]["formats"] == ["mp3"]
    assert models["fal-ai/ace-step/prompt-to-audio"]["formats"] == ["wav"]
    assert models["fal-ai/stable-audio-25/text-to-audio"]["supports_lyrics"] is False
    caps = provider.capabilities()
    assert caps["supports_lyrics"] is True
    assert caps["max_reference_images"] == 0


def test_generate_happy_path_with_stubbed_queue(monkeypatch):
    import plugins.music_gen.fal as fal_mod

    class _Handle:
        pass

    class _Client:
        def __init__(self):
            self.seen = None

        def submit(self, endpoint, *, arguments, headers):
            self.seen = (endpoint, arguments)
            return _Handle()

    client = _Client()
    monkeypatch.setattr(fal_mod, "_load_fal_client", lambda: client)
    monkeypatch.setattr(fal_mod, "_fal_music_available", lambda: True)
    monkeypatch.setattr("tools.fal_common.wait_fal_queue_result",
                        lambda handle: {"audio": {"url": "https://cdn/x.mp3"}, "lyrics": "la la"})
    monkeypatch.setattr(fal_mod, "save_url_music", lambda url, prefix: "/tmp/fake-x.mp3")
    monkeypatch.setattr(fal_mod, "_resolve_managed_fal_music_gateway", lambda: None)

    provider = fal_mod.FALMusicGenProvider()
    result = provider.generate("a song", lyrics="la la", duration=30)
    assert result["success"] is True
    assert result["audio"] == "/tmp/fake-x.mp3"
    assert result["lyrics"] == "la la"
    assert client.seen[0] == "fal-ai/minimax-music/v2.6"
    assert client.seen[1]["lyrics"] == "la la"


def test_generate_without_backend_errors(monkeypatch):
    import plugins.music_gen.fal as fal_mod

    monkeypatch.setattr(fal_mod, "_fal_music_available", lambda: False)
    result = fal_mod.FALMusicGenProvider().generate("a song")
    assert result["success"] is False
    assert result["error_type"] == "auth_required"


def test_generate_missing_prompt_errors():
    from plugins.music_gen.fal import FALMusicGenProvider

    result = FALMusicGenProvider().generate("   ")
    assert result["success"] is False
    assert result["error_type"] == "missing_prompt"


def test_register_wires_provider():
    import plugins.music_gen.fal as fal_mod

    class _Ctx:
        def register_music_gen_provider(self, provider):
            music_gen_registry.register_provider(provider)

    fal_mod.register(_Ctx())
    assert any(p.name == "fal" for p in music_gen_registry.list_providers())
