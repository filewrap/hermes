"""Tests for the Google (Lyria) music gen plugin — REST parsing and dispatch."""

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
    def __init__(self, *, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)

    def close(self):
        pass


def _lyria_payload(*, audio_b64=None, lyrics_text=None, block_reason=None):
    parts = []
    if audio_b64 is not None:
        parts.append({"inlineData": {"mimeType": "audio/mpeg", "data": audio_b64}})
    if lyrics_text is not None:
        parts.append({"text": lyrics_text})
    payload: dict = {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}
    if block_reason:
        payload["promptFeedback"] = {"blockReason": block_reason}
    return payload


def test_canonical_b64_repads_and_rejects():
    from plugins.music_gen.google import _canonical_b64

    assert _canonical_b64("SUQz") == "SUQz"
    assert _canonical_b64("SUQ") is not None  # missing padding is repaired
    assert _canonical_b64("!!!") is None


def test_terminal_reason_from_block():
    from plugins.music_gen.google import _terminal_no_audio_reason

    assert _terminal_no_audio_reason(_lyria_payload(block_reason="SAFETY")) == "prompt blocked (SAFETY)"
    assert _terminal_no_audio_reason(_lyria_payload()) is None


def test_generate_happy_path(monkeypatch, tmp_path):
    import plugins.music_gen.google as g

    provider = g.GoogleMusicGenProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "dummy-key")
    audio = base64.b64encode(b"FAKEAUDIO").decode()
    response = _FakeResponse(payload=_lyria_payload(audio_b64=audio, lyrics_text="la la"))
    captured = {}

    def _fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return response

    monkeypatch.setattr("requests.post", _fake_post)
    result = provider.generate("a lofi song", lyrics="la la")
    assert result["success"] is True
    assert captured["url"].endswith("/models/lyria-3-clip-preview:generateContent")
    assert captured["headers"]["x-goog-api-key"] == "dummy-key"
    assert captured["body"]["generationConfig"]["responseModalities"] == ["AUDIO", "TEXT"]
    assert "Lyrics:\nla la" in captured["body"]["contents"][0]["parts"][0]["text"]
    assert result["lyrics"] == "la la"
    from pathlib import Path
    assert Path(result["audio"]).read_bytes() == b"FAKEAUDIO"


def test_generate_blocked_prompt_errors_without_retry(monkeypatch):
    import plugins.music_gen.google as g

    provider = g.GoogleMusicGenProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "dummy-key")
    calls = []
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: calls.append(1) or _FakeResponse(
            payload=_lyria_payload(block_reason="SAFETY")))
    result = provider.generate("a song")
    assert result["success"] is False
    assert "prompt blocked (SAFETY)" in result["error"]
    assert len(calls) == 1


def test_generate_retries_transient_text_only_once(monkeypatch):
    import plugins.music_gen.google as g

    provider = g.GoogleMusicGenProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "dummy-key")
    audio = base64.b64encode(b"FAKEAUDIO").decode()
    responses = [
        _FakeResponse(payload=_lyria_payload(lyrics_text="only text")),
        _FakeResponse(payload=_lyria_payload(audio_b64=audio)),
    ]
    calls = []
    monkeypatch.setattr("requests.post", lambda *a, **k: calls.append(1) or responses.pop(0))
    result = provider.generate("a song")
    assert result["success"] is True
    assert len(calls) == 2


def test_generate_wav_rejected_for_clip(monkeypatch):
    import plugins.music_gen.google as g

    provider = g.GoogleMusicGenProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "dummy-key")
    result = provider.generate("a song", format="wav")
    assert result["success"] is False
    assert result["error_type"] == "capability_unsupported"


def test_generate_without_key_errors(monkeypatch):
    import plugins.music_gen.google as g

    provider = g.GoogleMusicGenProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "")
    result = provider.generate("a song")
    assert result["success"] is False
    assert result["error_type"] == "missing_credentials"


def test_catalog_and_caps():
    from plugins.music_gen.google import GoogleMusicGenProvider

    provider = GoogleMusicGenProvider()
    assert provider.default_model() == "lyria-3-clip-preview"
    assert any(m["id"] == "lyria-3-pro-preview" for m in provider.list_models())
    caps = provider.capabilities()
    assert caps["max_reference_images"] == 10
    assert caps["supports_lyrics"] is True
    assert caps["supports_duration"] is False
