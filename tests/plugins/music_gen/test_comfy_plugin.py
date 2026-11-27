"""Tests for the ComfyUI music gen plugin — workflow handling and local protocol."""

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


def _config(**overrides):
    base = {
        "mode": "local",
        "base_url": "http://127.0.0.1:8188",
        "workflow": {"3": {"inputs": {"text": ""}}, "9": {"inputs": {}}},
        "prompt_node_id": "3",
        "output_node_id": "9",
    }
    base.update(overrides)
    return base


class _FakeResponse:
    def __init__(self, *, status_code=200, payload=None, body=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = body
        self.text = body.decode("utf-8", "replace")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError(f"HTTP {self.status_code}")


class _FakeSession:
    """Canned ComfyUI local server: submit → history with one audio output → bytes."""

    def __init__(self, *, history=None):
        self.history = history
        self.posts = []
        self.gets = []
        self.closed = False

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/prompt"):
            return _FakeResponse(payload={"prompt_id": "abc"})
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if "/history/" in url:
            payload = self.history if self.history is not None else {
                "abc": {"status": {"completed": True},
                        "outputs": {"9": {"audio": [{"filename": "out.mp3", "subfolder": "",
                                                     "type": "output"}]}}}}
            return _FakeResponse(payload=payload)
        if url.endswith("/view"):
            return _FakeResponse(body=b"FAKEAUDIO")
        raise AssertionError(f"unexpected GET {url}")

    def close(self):
        self.closed = True


def test_read_config_overlay_wins(monkeypatch):
    import plugins.music_gen.comfy as c

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"music_gen": {"comfy": {"mode": "local", "prompt_node_id": "1",
                                         "music": {"prompt_node_id": "3"}}}})
    config = c._read_config()
    assert config["prompt_node_id"] == "3"
    assert config["mode"] == "local"


def test_load_workflow_from_path(tmp_path):
    import plugins.music_gen.comfy as c

    workflow_path = tmp_path / "wf.json"
    workflow_path.write_text(json.dumps({"3": {"inputs": {"text": ""}}}))
    assert c._load_workflow({"workflow_path": str(workflow_path)}) == {"3": {"inputs": {"text": ""}}}
    with pytest.raises(ValueError, match="music_gen.comfy.workflow or music_gen.comfy.workflow_path is required"):
        c._load_workflow({})


def test_set_workflow_input_missing_node():
    import plugins.music_gen.comfy as c

    with pytest.raises(ValueError, match='missing node "7"'):
        c._set_workflow_input({}, "7", "text", "x")


def test_history_entry_and_output_files():
    import plugins.music_gen.comfy as c

    assert c._history_entry({"outputs": {"9": {}}}, "abc") == {"outputs": {"9": {}}}
    assert c._history_entry({"abc": {"outputs": {"9": {}}}}, "abc") == {"outputs": {"9": {}}}
    entry = {"outputs": {"9": {"audio": [{"filename": "a.mp3"}],
                               "images": [{"filename": "i.png"}]}}}
    assert c._output_files(entry, "9") == [{"node_id": "9", "filename": "a.mp3"}]
    assert c._output_files(entry, None) == [{"node_id": "9", "filename": "a.mp3"}]


def test_generate_local_happy_path(monkeypatch, tmp_path):
    import plugins.music_gen.comfy as c

    session = _FakeSession()
    monkeypatch.setattr("requests.Session", lambda: session)
    monkeypatch.setattr(c, "_read_config", lambda: _config())
    result = c.ComfyMusicGenProvider().generate("a lofi song")
    assert result["success"] is True
    assert result["prompt_id"] == "abc"
    from pathlib import Path
    assert Path(result["audio"]).read_bytes() == b"FAKEAUDIO"
    assert session.closed
    submit = next(url for url, _ in session.posts if url.endswith("/prompt"))
    assert submit == "http://127.0.0.1:8188/prompt"


def test_generate_missing_prompt_node_errors(monkeypatch):
    import plugins.music_gen.comfy as c

    monkeypatch.setattr(c, "_read_config", lambda: _config(prompt_node_id=""))
    result = c.ComfyMusicGenProvider().generate("a song")
    assert result["success"] is False
    assert result["error_type"] == "missing_config"


def test_generate_missing_workflow_errors(monkeypatch):
    import plugins.music_gen.comfy as c

    monkeypatch.setattr(c, "_read_config", lambda: {"mode": "local", "prompt_node_id": "3"})
    result = c.ComfyMusicGenProvider().generate("a song")
    assert result["success"] is False
    assert result["error_type"] == "missing_config"


def test_generate_no_audio_outputs_errors(monkeypatch):
    import plugins.music_gen.comfy as c

    session = _FakeSession(history={"abc": {"status": {"completed": True}, "outputs": {"9": {}}}})
    monkeypatch.setattr("requests.Session", lambda: session)
    monkeypatch.setattr(c, "_read_config", lambda: _config())
    result = c.ComfyMusicGenProvider().generate("a song")
    assert result["success"] is False
    assert result["error_type"] == "empty_response"


def test_is_available_requires_workflow_and_prompt_node(monkeypatch):
    import plugins.music_gen.comfy as c

    provider = c.ComfyMusicGenProvider()
    monkeypatch.setattr(c, "_read_config", lambda: _config())
    assert provider.is_available() is True
    monkeypatch.setattr(c, "_read_config", lambda: {"mode": "local"})
    assert provider.is_available() is False


def test_catalog_and_caps():
    from plugins.music_gen.comfy import ComfyMusicGenProvider

    provider = ComfyMusicGenProvider()
    assert provider.default_model() == "workflow"
    caps = provider.capabilities()
    assert caps["max_reference_images"] == 1
    assert caps["supports_lyrics"] is False
    assert caps["supports_format"] is False
