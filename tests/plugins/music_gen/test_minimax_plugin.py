"""Tests for the MiniMax music gen plugin — hex/SSE parsing and dispatch."""

from __future__ import annotations

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
    def __init__(self, *, status_code=200, headers=None, lines=None, body=b"", payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._lines = lines or []
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self._payload = payload

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)

    def close(self):
        pass


def test_decode_hex_roundtrip():
    from plugins.music_gen.minimax import _decode_hex

    assert _decode_hex("494433", 100) == b"ID3"
    with pytest.raises(ValueError):
        _decode_hex("zz", 100)
    with pytest.raises(ValueError):
        _decode_hex("00" * 101, 100)


def test_assert_base_resp_rejects_nonzero():
    from plugins.music_gen.minimax import _assert_base_resp

    _assert_base_resp({"base_resp": {"status_code": 0}}, "x")
    with pytest.raises(ValueError, match="MiniMax music"):
        _assert_base_resp({"base_resp": {"status_code": 1004, "status_msg": "no quota"}}, "MiniMax music")


def test_parse_json_payload_hex_and_url():
    from plugins.music_gen.minimax import _parse_json_payload

    hex_audio, url, lyrics, task_id = _parse_json_payload(
        {"data": {"audio": "494433"}, "lyrics": "la", "task_id": "t1"})
    assert hex_audio == "494433"
    assert url is None
    assert lyrics == "la"
    assert task_id == "t1"
    hex_audio, url, lyrics, task_id = _parse_json_payload(
        {"data": {"audio": "https://cdn/x.mp3"}})
    assert hex_audio is None
    assert url == "https://cdn/x.mp3"


def test_read_sse_track_assembles_hex_frames():
    from plugins.music_gen.minimax import _read_sse_track

    lines = [
        'data: {"data": {"audio": "4944", "status": 1}, "base_resp": {"status_code": 0}}',
        'data: {"data": {"audio": "33", "status": 2}, "base_resp": {"status_code": 0}}',
        "",
    ]
    assert _read_sse_track(_FakeResponse(lines=lines), 100) == b"ID3"


def test_read_sse_track_requires_completion():
    from plugins.music_gen.minimax import _read_sse_track

    lines = ['data: {"data": {"audio": "4944", "status": 1}, "base_resp": {"status_code": 0}}']
    with pytest.raises(ValueError, match="without completion"):
        _read_sse_track(_FakeResponse(lines=lines), 100)


def test_generate_sse_happy_path(monkeypatch, tmp_path):
    import plugins.music_gen.minimax as mm

    provider = mm.MiniMaxMusicGenProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "dummy-key")
    lines = [
        'data: {"data": {"audio": "494433", "status": 2}, "base_resp": {"status_code": 0}}',
    ]
    response = _FakeResponse(headers={"Content-Type": "text/event-stream"}, lines=lines)
    captured = {}

    def _fake_post(url, *, headers, json, timeout, stream):
        captured["url"] = url
        captured["body"] = json
        return response

    monkeypatch.setattr("requests.post", _fake_post)
    result = provider.generate("a song", lyrics="la la")
    assert result["success"] is True
    assert captured["url"] == "https://api.minimax.io/v1/music_generation"
    assert captured["body"]["output_format"] == "hex"
    assert captured["body"]["lyrics"] == "la la"
    assert (tmp_path / "cache" / "generated" / "tool-music-generation").exists()
    from pathlib import Path
    assert Path(result["audio"]).read_bytes() == b"ID3"


def test_generate_json_url_payload(monkeypatch):
    import plugins.music_gen.minimax as mm

    provider = mm.MiniMaxMusicGenProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "dummy-key")
    response = _FakeResponse(
        headers={"Content-Type": "application/json"},
        payload={"data": {"audio": "https://cdn/x.mp3"}, "lyrics": "la"},
        body=b"{}")
    monkeypatch.setattr("requests.post", lambda *a, **k: response)
    monkeypatch.setattr(mm, "save_url_music", lambda url, prefix: "/tmp/mm-x.mp3")
    result = provider.generate("a song")
    assert result["success"] is True
    assert result["audio"] == "/tmp/mm-x.mp3"
    assert result["lyrics"] == "la"


def test_generate_without_key_errors(monkeypatch):
    import plugins.music_gen.minimax as mm

    provider = mm.MiniMaxMusicGenProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "")
    result = provider.generate("a song")
    assert result["success"] is False
    assert result["error_type"] == "missing_credentials"


def test_generate_lyrics_plus_instrumental_rejected(monkeypatch):
    import plugins.music_gen.minimax as mm

    provider = mm.MiniMaxMusicGenProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "dummy-key")
    result = provider.generate("a song", lyrics="la", instrumental=True)
    assert result["success"] is False
    assert result["error_type"] == "capability_unsupported"


def test_generate_wav_rejected(monkeypatch):
    import plugins.music_gen.minimax as mm

    provider = mm.MiniMaxMusicGenProvider()
    monkeypatch.setattr(provider, "_api_key", lambda: "dummy-key")
    result = provider.generate("a song", format="wav")
    assert result["success"] is False
    assert result["error_type"] == "capability_unsupported"


def test_catalog_and_caps():
    from plugins.music_gen.minimax import MiniMaxMusicGenProvider

    provider = MiniMaxMusicGenProvider()
    assert provider.default_model() == "music-2.6"
    assert any(m["id"] == "music-cover" for m in provider.list_models())
    caps = provider.capabilities()
    assert caps["supports_lyrics"] is True
    assert caps["supports_duration"] is False
    assert caps["formats"] == ["mp3"]
