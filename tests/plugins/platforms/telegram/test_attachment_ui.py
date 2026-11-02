from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.telegram_personal_settings import (
    DEFAULT_ATTACHMENT_DOWNLOAD_THRESHOLD_BYTES,
    DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES,
    ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES,
    _validated_changes,
    load_personal_settings,
    save_personal_settings,
)
from plugins.platforms.telegram.attachment_ui import TelegramAttachmentUI, format_size


@dataclass(frozen=True)
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    thread_id: str | None = None
    chat_type: str = "dm"


class FakeMessage:
    def __init__(self, *, size=12, name="report.pdf", mime="application/pdf", caption="See attached", message_id=55):
        self.id = message_id
        self.message = caption
        self.file = SimpleNamespace(size=size, name=name, mime_type=mime)
        self.document = object()
        self.photo = None


class FakeAdapter:
    def __init__(self, home):
        self.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: home)
        self.reactions = []
        self._client = None

    async def send_reaction(self, chat_id, message_id, emoji):
        self.reactions.append((chat_id, message_id, emoji))


class Callback:
    def __init__(self):
        self.answers = []

    async def answer(self, text=None):
        self.answers.append(text or "")


def test_download_preferences_validate_and_default_to_200_mib(tmp_path):
    defaults = load_personal_settings(tmp_path, "123")
    assert defaults["attachment_download_mode"] == "auto"
    assert defaults["attachment_auto_download_threshold_bytes"] == DEFAULT_ATTACHMENT_DOWNLOAD_THRESHOLD_BYTES
    assert defaults["attachment_explicit_download_limit_bytes"] == DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES
    assert defaults["reactions_enabled"] is True
    assert defaults["text_reactions_enabled"] is False
    assert _validated_changes({"attachment_download_mode": "explicit"})["attachment_download_mode"] == "explicit"
    assert _validated_changes({"attachment_auto_download_threshold_bytes": 50 * 1024 * 1024})
    assert _validated_changes({"attachment_explicit_download_limit_bytes": 4096 * 1024 * 1024})
    with pytest.raises(ValueError):
        _validated_changes({"attachment_download_mode": "always"})
    with pytest.raises(ValueError):
        _validated_changes({"attachment_auto_download_threshold_bytes": -1})
    with pytest.raises(ValueError):
        _validated_changes({"attachment_explicit_download_limit_bytes": 300 * 1024 * 1024})


@pytest.mark.asyncio
async def test_authorized_intake_acks_and_delivers_caption_mime_type_size_without_downloading(monkeypatch, tmp_path):
    adapter = FakeAdapter(tmp_path)
    ui = TelegramAttachmentUI(adapter)
    message = FakeMessage(size=300 * 1024 * 1024, caption="look here")
    monkeypatch.setattr("plugins.platforms.telegram.attachment_ui.effective_download_cap", lambda: 128 * 1024 * 1024)

    intake = await ui.prepare_inbound(Source(), [message], authorized=True, session_key="profile-a:dm:123")

    assert adapter.reactions == [("123", 55, "👀")]
    assert "caption: look here" in intake.note
    assert "MIME application/pdf" in intake.note
    assert "size 300.0 MiB" in intake.note
    assert "deferred" in intake.note
    assert intake.attachments[0].callback_data.startswith("tdl:")
    prompt = ui.download_prompt(intake)
    assert prompt is not None and "2.0 GiB" in prompt[0] and "128.0 MiB" in prompt[0]


@pytest.mark.asyncio
async def test_unauthorized_intake_fails_before_ack_or_settings_or_download(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ui = TelegramAttachmentUI(adapter)
    with pytest.raises(PermissionError):
        await ui.prepare_inbound(Source(), FakeMessage(), authorized=False)
    assert adapter.reactions == []


@pytest.mark.asyncio
async def test_disabling_general_reactions_suppresses_ack_but_keeps_attachment_metadata(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ui = TelegramAttachmentUI(adapter)
    save_personal_settings(tmp_path, "123", {"reactions_enabled": False})

    intake = await ui.prepare_inbound(Source(), FakeMessage(), authorized=True, session_key="session-A")

    assert adapter.reactions == []
    assert len(intake.attachments) == 1
    assert "MIME application/pdf" in intake.note


@pytest.mark.asyncio
async def test_download_callback_is_bound_to_profile_user_chat_topic_and_session(monkeypatch, tmp_path):
    adapter = FakeAdapter(tmp_path)
    ui = TelegramAttachmentUI(adapter)
    source = Source()
    message = FakeMessage()
    callback_data = ui._new_download_request(source, message, tmp_path, "session-A")
    callback = Callback()

    wrong_session = await ui.handle_callback(callback, source, callback_data, authorized=True, session_key="session-B")
    assert wrong_session["expired"] is True
    assert callback.answers == ["This download belongs to a different Telegram session."]
    assert message in [request["message"] for request in ui._pending.values()]

    denied = await ui.handle_callback(callback, source, callback_data, authorized=False, session_key="session-A")
    assert denied == {"handled": True, "authorized": False}
    assert len(callback.answers) == 1


@pytest.mark.asyncio
async def test_explicit_download_streams_to_private_cache_and_consumes_one_shot(monkeypatch, tmp_path):
    from gateway.platforms import base
    from tools import credential_files

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(mode=0o700)
    monkeypatch.setattr("plugins.platforms.telegram.attachment_ui.effective_download_cap", lambda: 1024)
    monkeypatch.setattr(base, "get_document_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(credential_files, "to_agent_visible_cache_path", lambda path: path)

    class StreamingClient:
        def __init__(self):
            self.received_file = None

        async def download_media(self, message, *, file, progress_callback):
            self.received_file = file
            content = b"PDF contents"
            progress_callback(len(content), len(content))
            file.write(content)
            return str(cache_dir / "downloaded.pdf")

    client = StreamingClient()
    adapter = FakeAdapter(tmp_path)
    adapter._client = client
    ui = TelegramAttachmentUI(adapter)
    source = Source()
    message = FakeMessage(size=12)
    callback_data = ui._new_download_request(source, message, tmp_path, "session-A")
    callback = Callback()

    result = await ui.handle_callback(callback, source, callback_data, authorized=True, session_key="session-A")

    assert result["handled"] is True
    assert result["media"].kind == "document"
    saved = Path(result["media"].path)
    assert saved.read_bytes() == b"PDF contents"
    assert saved.stat().st_mode & 0o777 == 0o600
    assert client.received_file is not None
    assert callback.answers == ["Downloading attachment…"]
    assert callback_data.partition(":")[2] not in ui._pending


@pytest.mark.asyncio
async def test_300_mib_explicit_stream_succeeds_without_allocating_payload(monkeypatch, tmp_path):
    from gateway.platforms import base
    from tools import credential_files

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(mode=0o700)
    monkeypatch.setattr("plugins.platforms.telegram.attachment_ui.effective_download_cap", lambda: 128 * 1024 * 1024)
    monkeypatch.setattr(base, "get_document_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(credential_files, "to_agent_visible_cache_path", lambda path: path)
    size = 300 * 1024 * 1024

    class SparseStreamingClient:
        async def download_media(self, message, *, file, progress_callback):
            # Seek creates a sparse logical 300 MiB file instead of allocating a payload buffer.
            file.seek(size - 1)
            file.write(b"x")
            progress_callback(size, size)
            return str(cache_dir / "ignored-by-cache.pdf")

    adapter = FakeAdapter(tmp_path)
    adapter._client = SparseStreamingClient()
    ui = TelegramAttachmentUI(adapter)
    source = Source()
    message = FakeMessage(size=size)
    intake = await ui.prepare_inbound(source, message, authorized=True, session_key="session-A")
    assert intake.attachments[0].deferred is True
    assert intake.attachments[0].download_limit_bytes == DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES
    callback_data = intake.attachments[0].callback_data

    result = await ui.handle_callback(Callback(), source, callback_data, authorized=True, session_key="session-A")

    assert result["media"].path
    saved = Path(result["media"].path)
    assert saved.stat().st_size == size
    assert saved.stat().st_blocks * 512 < 1024 * 1024


@pytest.mark.asyncio
async def test_stream_rejects_over_limit_chunk_and_cleans_partial_file(monkeypatch, tmp_path):
    from gateway.platforms import base
    from tools import credential_files

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(mode=0o700)
    monkeypatch.setattr(base, "get_document_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(credential_files, "to_agent_visible_cache_path", lambda path: path)

    class OversizedClient:
        async def download_media(self, message, *, file, progress_callback):
            file.write(b"12345678901")

    adapter = FakeAdapter(tmp_path)
    adapter._client = OversizedClient()
    ui = TelegramAttachmentUI(adapter)
    source = Source()
    message = FakeMessage(size=8)
    callback_data = ui._new_download_request(source, message, tmp_path, "session-A", max_bytes=10)

    result = await ui.handle_callback(Callback(), source, callback_data, authorized=True, session_key="session-A")

    assert "10 B" in result["error"]
    assert list(cache_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_declared_size_must_fit_available_disk_budget(monkeypatch, tmp_path):
    from gateway.platforms import base
    from plugins.platforms.telegram import attachment_ui

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(mode=0o700)
    monkeypatch.setattr(base, "get_document_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(attachment_ui.shutil, "disk_usage", lambda _path: SimpleNamespace(free=7, total=7, used=0))
    adapter = FakeAdapter(tmp_path)
    adapter._client = object()
    ui = TelegramAttachmentUI(adapter)

    with pytest.raises(OSError, match="disk space"):
        await ui._stream_to_cache(FakeMessage(size=8), max_bytes=100)
    assert list(cache_dir.iterdir()) == []


def test_explicit_download_limits_are_isolated_between_users(tmp_path):
    home = tmp_path / "profile"
    a_bytes, b_bytes = ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES[1], ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES[0]
    save_personal_settings(home, "101", {"attachment_explicit_download_limit_bytes": a_bytes})
    save_personal_settings(home, "202", {"attachment_explicit_download_limit_bytes": b_bytes})
    adapter = FakeAdapter(home)
    ui = TelegramAttachmentUI(adapter)

    request_a1 = ui._new_download_request(Source(user_id="101"), FakeMessage(), home, "A")
    request_b = ui._new_download_request(Source(user_id="202"), FakeMessage(), home, "B")
    request_a2 = ui._new_download_request(Source(user_id="101"), FakeMessage(), home, "A2")

    assert [ui._pending[token.partition(":")[2]]["max_bytes"] for token in (request_a1, request_b, request_a2)] == [a_bytes, b_bytes, a_bytes]


def test_only_explicit_operator_cap_restricts_personal_stream_limit(monkeypatch, tmp_path):
    import hermes_constants
    from plugins.platforms.telegram import attachment_ui

    config_home = tmp_path / "operator"
    config_home.mkdir()
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: config_home)
    monkeypatch.setattr(attachment_ui, "effective_download_cap", lambda: 128 * 1024 * 1024)
    assert attachment_ui.effective_explicit_download_limit({}) == DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES

    (config_home / "config.yaml").write_text("gateway:\n  max_inbound_media_bytes: 268435456\n", encoding="utf-8")
    monkeypatch.setattr(attachment_ui, "effective_download_cap", lambda: 256 * 1024 * 1024)
    assert attachment_ui.effective_explicit_download_limit({}) == 256 * 1024 * 1024


@pytest.mark.asyncio
async def test_album_intake_is_bounded_to_ten_and_uses_album_ack(monkeypatch, tmp_path):
    adapter = FakeAdapter(tmp_path)
    ui = TelegramAttachmentUI(adapter)
    monkeypatch.setattr("plugins.platforms.telegram.attachment_ui.effective_download_cap", lambda: 1024)
    messages = [FakeMessage(message_id=index + 1) for index in range(12)]

    intake = await ui.prepare_inbound(Source(), messages, authorized=True, session_key="album-session")

    assert len(intake.attachments) == 10
    assert len(adapter.reactions) == 10
    assert all(item[2] == "🆒" for item in adapter.reactions)
    assert "first 10 album attachments" in intake.note


def test_format_size_is_honest_and_human_readable():
    assert format_size(0) == "0 B"
    assert format_size(1024) == "1.0 KiB"
