"""Tests for the OpenRouter music gen plugin — SSE parsing and dispatch."""

from __future__ import annotations

import base64
import json

import pytest

from agent import music_gen_registry


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch, tmp_path):
    music_gen_registry._reset_for_tests()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield
    music_gen_registry._reset_for_tests()


class _FakeResponse:
    def __init__(self, *, status_code=200, lines=None, text=""):
        self.status_code = status_code
        self._lines = lines or []
        self.text = text

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def json(self):
        return json.loads(self.text)

    def close(self):
        pass


def _sse_data(payload: dict) -> str:
    return "data: " + json.dumps(payload)


def _audio_frame(b64: str, transcript: str | None = None) -> str:
    audio: dict = {"data": b64}
    if transcript is not None:
        audio["transcript"] = transcript
    return _sse_data({"choices": [{"delta": {"audio": audio}}]})


def test_read_audio_stream_joins_split_quartets():
    from plugins.music_gen.openrouter import _read_audio_stream

    full = base64.b64encode(b"FAKEAUDIOSTREAM").decode()
    # Split mid-quartet so the remainder logic is exercised.
    lines = [_audio_frame(full[:5], "la"), _audio_frame(full[5:]), "data: [DONE]"]
    audio, transcript = _read_audio_stream(_FakeResponse(lines=lines), 100)
    assert audio == b"FAKEAUDIOSTREAM"
    assert transcript == "la"


def test_read_audio_stream_rejects_stream_error():
    from plugins.music_gen.openrouter import _read_audio_stream

    lines = [_sse_data({"error": {"message": "no credits"}})]
    with pytest.raises(ValueError, match="no credits"):
        _read_audio_stream(_FakeResponse(lines=lines), 100)


def test_read_audio_stream_requires_done():
    from plugins.music_gen.openrouter import _read_audio_stream

    with pytest.raises(ValueError, match="before completion"):
        _read_audio_stream(_FakeResponse(lines=[_audio_frame("SUQz")]), 100)


def test_generate_happy_path(monkeypatch, tmp_path):
    import plugins.music_gen.openrouter as o

    provider = o.OpenRouterMusicGenProvider()
    monkeypatch.setattr(provider, "_credentials", lambda: ("dummy-key", "https://openrouter.ai/api/v1"))
    full = base64.b64encode(b"FAKEAUDIO").decode()
    response = _FakeResponse(lines=[_audio_frame(full, "la la"), "data: [DONE]"])
    captured = {}

    def _fake_post(url, *, headers, json, timeout, stream):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return response

    monkeypatch.setattr("requests.post", _fake_post)
    result = provider.generate("a song", lyrics="la la", duration=30)
    assert result["success"] is True
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer dummy-key"
    assert captured["body"]["modalities"] == ["text", "audio"]
    assert captured["body"]["audio"] == {"format": "mp3"}
    assert "Target duration: about 30 seconds." in captured["body"]["messages"][0]["content"]
    assert result["lyrics"] == "la la"
    from pathlib import Path
    assert Path(result["audio"]).read_bytes() == b"FAKEAUDIO"


def test_generate_rejects_two_reference_images(monkeypatch):
    import plugins.music_gen.openrouter as o

    provider = o.OpenRouterMusicGenProvider()
    monkeypatch.setattr(provider, "_credentials", lambda: ("dummy-key", "https://x"))
    result = provider.generate(
        "a song",
        reference_image_urls=["https://example.com/a.png", "https://example.com/b.png"])
    assert result["success"] is False
    assert result["error_type"] == "capability_unsupported"


def test_generate_without_key_errors(monkeypatch):
    import plugins.music_gen.openrouter as o

    provider = o.OpenRouterMusicGenProvider()
    monkeypatch.setattr(provider, "_credentials", lambda: ("", "https://x"))
    result = provider.generate("a song")
    assert result["success"] is False
    assert result["error_type"] == "missing_credentials"


def test_catalog_and_caps():
    from plugins.music_gen.openrouter import OpenRouterMusicGenProvider

    provider = OpenRouterMusicGenProvider()
    assert provider.default_model() == "google/lyria-3-pro-preview"
    assert any(m["id"] == "google/lyria-3-clip-preview" for m in provider.list_models())
    caps = provider.capabilities()
    assert caps["max_reference_images"] == 1
    assert caps["max_duration"] == 180
