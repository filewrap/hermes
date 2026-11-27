"""Large Telegram downloads stream through Telethon into isolated private files."""
from __future__ import annotations

import asyncio
import errno
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.client.downloads import DownloadMethods

from plugins.platforms.telegram import client_operations as operations_module
from plugins.platforms.telegram.client_operations import TelegramClientOperations


RESERVE = 16 * 1024 * 1024
CHUNK = 256 * 1024


def source(*, profile="alpha", chat_id="-1000000000321", thread_id=None, user_id="7"):
    return SimpleNamespace(platform="telegram", user_id=user_id, chat_id=chat_id,
                           thread_id=thread_id, profile=profile, is_bot=False,
                           is_guest=False, chat_type="group", message_id="55")


def media_message(*, message_id=81, size=1, filename="report.txt", mime="text/plain", caption="caption"):
    document = SimpleNamespace(id=702, size=size, mime_type=mime,
                               attributes=[SimpleNamespace(file_name=filename)])
    return SimpleNamespace(id=message_id, message=caption, date=None, sender_id=12,
                           media=SimpleNamespace(document=document), file=None)


class FakeAdapter:
    def __init__(self, home, downloader=None):
        self.homes = {"alpha": Path(home), "beta": Path(home) / "profile-beta"}
        self.allowed = True
        self.owner = True
        self._client = SimpleNamespace(download_media=downloader or self.download_media,
                                       get_messages=SimpleNamespace())
        self._client.get_messages = _AsyncEmpty()

    async def download_media(self, message, file, progress_callback=None):
        Path(file).write_bytes(b"x")
        if progress_callback:
            await progress_callback(1, getattr(message.media.document, "size", None))
        return file

    def _authorized(self, _source):
        return self.allowed

    def _is_owner_user(self, user_id, _source):
        return self.owner and str(user_id) == "7"

    def _settings_home(self, src):
        return self.homes.get(src.profile, self.homes["alpha"])

    async def _input_peer_for(self, _chat_id):
        return object()

    @staticmethod
    def _message_thread_id(message):
        reply = getattr(message, "reply_to", None)
        return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)


class _AsyncEmpty:
    async def __call__(self, *_args, **_kwargs):
        return []


class NativeStreamingClient:
    """Fake only the MTProto iterator; exercise Telethon's real disk writer."""

    def __init__(self, size, chunk=CHUNK):
        self.file_size = size
        self.chunk = chunk
        self.iterated = 0
        self.chunk_bytes = bytes(i % 251 for i in range(chunk))

    async def download_media(self, message, file, progress_callback=None):
        return await DownloadMethods._download_file(
            self, b"fake-input-location", file=file, part_size_kb=256,
            file_size=self.file_size, progress_callback=progress_callback)

    async def _iter_download(self, _location, *, request_size, **_kwargs):
        assert request_size == CHUNK
        remaining = self.file_size
        while remaining:
            n = min(self.chunk, remaining)
            self.iterated += 1
            yield self.chunk_bytes[:n]
            remaining -= n


def cache_root(home):
    return Path(home) / "cache" / "telegram-client-operations"


def assert_no_partial_files(home):
    root = cache_root(home)
    if root.exists():
        assert not list(root.glob(".telegram-download-*"))


@pytest.mark.asyncio
async def test_large_download_uses_native_telethon_streaming_and_keeps_metadata(tmp_path):
    # Above Telegram's former 25 MiB application ceiling, with bounded chunk allocations.
    size = 26 * 1024 * 1024 + 37
    native = NativeStreamingClient(size)
    adapter = FakeAdapter(tmp_path, native.download_media)
    ops = TelegramClientOperations(adapter)
    message = media_message(size=size, filename="archive.dat", mime="application/octet-stream",
                            caption="Quarterly files")
    ops.observe(source(), message)
    progress = []

    async def observe_progress(current, total):
        progress.append((current, total))

    result = await ops.request(source(), "files_download", {"message_id": message.id},
                               progress_callback=observe_progress)
    assert result["ok"], result
    data = result["data"]
    path = Path(data["cached_path"])
    assert path.stat().st_size == size and data["downloaded_bytes"] == size
    assert data["text"] == "Quarterly files"
    assert data["file_name"] == "archive.dat"
    assert data["mime_type"] == "application/octet-stream"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert native.iterated > 100 and len(progress) > 2
    assert progress[-1][0] == size
    expected_hash = hashlib.sha256(native.chunk_bytes * (size // CHUNK) +
                                   native.chunk_bytes[:size % CHUNK]).hexdigest()
    hasher = hashlib.sha256()
    with await asyncio.to_thread(path.open, "rb") as downloaded:
        for block in iter(lambda: downloaded.read(1024 * 1024), b""):
            hasher.update(block)
    assert hasher.hexdigest() == expected_hash
    assert_no_partial_files(tmp_path)


@pytest.mark.asyncio
async def test_multigib_metadata_and_sparse_stream_need_no_huge_allocation(tmp_path, monkeypatch):
    size = 3 * 1024**3 + 17
    chunks = []

    async def sparse_download(_message, file, progress_callback=None):
        with await asyncio.to_thread(open, file, "wb") as stream:
            for block in (b"first-small-chunk", b"second-small-chunk"):
                chunks.append(len(block))
                stream.write(block)
                if progress_callback:
                    await progress_callback(stream.tell(), size)
            stream.truncate(size)

    adapter = FakeAdapter(tmp_path, sparse_download)
    ops = TelegramClientOperations(adapter)
    message = media_message(size=size, filename="sparse.bin")
    ops.observe(source(), message)
    monkeypatch.setattr(operations_module.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(free=size + RESERVE + 1))

    result = await ops.request(source(), "files_download", {"message_id": message.id})
    assert result["ok"], result
    path = Path(result["data"]["cached_path"])
    assert result["data"]["downloaded_bytes"] == size
    assert path.stat().st_size == size
    assert path.stat().st_blocks * 512 < 1024 * 1024
    assert chunks == [len(b"first-small-chunk"), len(b"second-small-chunk")]
    with await asyncio.to_thread(path.open, "rb") as stream:
        expected = b"first-small-chunksecond-small-chunk"
        assert stream.read(len(expected)) == expected
    assert_no_partial_files(tmp_path)


@pytest.mark.asyncio
async def test_insufficient_reserve_and_incomplete_download_remove_partials(tmp_path, monkeypatch):
    size = 8 * 1024 * 1024
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    message = media_message(size=size)
    ops.observe(source(), message)
    monkeypatch.setattr(operations_module.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(free=size + RESERVE - 1))
    result = await ops.request(source(), "files_download", {"message_id": message.id})
    assert result["error"]["code"] == "DOWNLOAD_STORAGE_ERROR"
    assert_no_partial_files(tmp_path)

    monkeypatch.setattr(operations_module.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(free=10 * size + RESERVE))

    async def short_download(_message, file, progress_callback=None):
        Path(file).write_bytes(b"short")
        if progress_callback:
            await progress_callback(5, size)

    adapter._client.download_media = short_download
    result = await ops.request(source(), "files_download", {"message_id": message.id})
    assert result["error"]["code"] == "DOWNLOAD_INCOMPLETE"
    assert_no_partial_files(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EDQUOT, errno.EFBIG])
async def test_storage_write_failure_is_reported_and_cleans_partial_file(tmp_path, error_number):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    message = media_message(size=16)
    ops.observe(source(), message)

    async def disk_full(_message, file, progress_callback=None):
        Path(file).write_bytes(b"partial")
        raise OSError(error_number, "storage failure")

    adapter._client.download_media = disk_full
    result = await ops.request(source(), "files_download", {"message_id": message.id})
    assert result["error"]["code"] == "DOWNLOAD_STORAGE_ERROR"
    assert_no_partial_files(tmp_path)


@pytest.mark.asyncio
async def test_download_cancellation_by_event_and_external_task_cleans_partials(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    message = media_message(size=16)
    ops.observe(source(), message)
    event = asyncio.Event()

    async def stop_by_event(_message, file, progress_callback=None):
        Path(file).write_bytes(b"partial")
        event.set()
        await progress_callback(7, 16)

    adapter._client.download_media = stop_by_event
    result = await ops.request(source(), "files_download", {"message_id": message.id},
                               cancel_event=event)
    assert result["error"]["code"] == "DOWNLOAD_CANCELLED"
    assert_no_partial_files(tmp_path)

    entered = asyncio.Event()

    async def block_until_cancelled(_message, file, progress_callback=None):
        Path(file).write_bytes(b"partial")
        entered.set()
        await asyncio.Event().wait()

    adapter._client.download_media = block_until_cancelled
    pending = asyncio.create_task(ops.request(source(), "files_download", {"message_id": message.id}))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert_no_partial_files(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_before_progress", [True, False])
async def test_admission_change_during_stream_or_before_commit_stops_and_removes_partial(
        tmp_path, revoke_before_progress):
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    message = media_message(size=16)
    ops.observe(source(), message)

    async def revoke_after_chunk(_message, file, progress_callback=None):
        Path(file).write_bytes(b"partial")
        await progress_callback(7, 16)
        adapter.allowed = False
        if revoke_before_progress:
            await progress_callback(8, 16)

    adapter._client.download_media = revoke_after_chunk
    result = await ops.request(source(), "files_download", {"message_id": message.id})
    assert result["error"]["code"] in {"DOWNLOAD_SCOPE_CHANGED", "FORBIDDEN"}
    assert_no_partial_files(tmp_path)


@pytest.mark.asyncio
async def test_no_progress_idle_timeout_cleans_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(operations_module, "DOWNLOAD_IDLE_TIMEOUT", 0.02)
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    message = media_message(size=16)
    ops.observe(source(), message)

    async def stalled(_message, file, progress_callback=None):
        Path(file).write_bytes(b"partial")
        if progress_callback:
            await progress_callback(7, 16)
        await asyncio.Event().wait()

    adapter._client.download_media = stalled
    result = await ops.request(source(), "files_download", {"message_id": message.id})
    assert result["error"]["code"] == "DOWNLOAD_TIMEOUT"
    assert_no_partial_files(tmp_path)


@pytest.mark.asyncio
async def test_profile_chat_and_topic_scope_alternation_and_admitted_nonowner_read(tmp_path):
    adapter = FakeAdapter(tmp_path)
    adapter.owner = False
    ops = TelegramClientOperations(adapter)
    original = source(user_id="8")
    variants = [
        source(profile="beta", user_id="8"),
        source(chat_id="-1000000000999", user_id="8"),
        source(thread_id="42", user_id="8"),
    ]
    ops.observe(original, media_message(size=len(b"allowed read")))

    async def content_for(_message, file, progress_callback=None):
        Path(file).write_bytes(b"allowed read")
        if progress_callback:
            await progress_callback(12, 12)

    adapter._client.download_media = content_for
    for other in variants:
        first = await ops.request(original, "files_download", {"message_id": 81})
        unavailable_in_other_scope = await ops.request(other, "files_download", {"message_id": 81})
        last = await ops.request(original, "files_download", {"message_id": 81})
        assert first["ok"] and last["ok"]
        assert not unavailable_in_other_scope["ok"]
        assert Path(first["data"]["cached_path"]).read_bytes() == b"allowed read"
        assert Path(last["data"]["cached_path"]).read_bytes() == b"allowed read"
        # The same global Telegram message ID in another profile/chat/topic cannot borrow A's observation.
        assert_no_partial_files(tmp_path)


@pytest.mark.asyncio
async def test_get_message_allows_chain_reply_file_from_topic_less_source(tmp_path):
    """A file sent as a reply inside a plain-group reply chain must stay
    readable from a topic-less source: the chain root is not a topic, so the
    topic-scope check must not refuse it — while a real topic source still
    enforces the match."""
    adapter = FakeAdapter(tmp_path)
    ops = TelegramClientOperations(adapter)
    chain_file = media_message(message_id=81)
    chain_file.reply_to = SimpleNamespace(
        reply_to_msg_id=80, reply_to_top_id=33392, forum_topic=False)
    chain_file.chat_id = "-1000000000321"

    async def get_messages(peer, *, ids=None):
        return [chain_file]

    adapter._client.get_messages = get_messages
    found = await ops._get_message(source(thread_id=None), 81)
    assert found is chain_file
    with pytest.raises(ValueError, match="current Telegram topic"):
        await ops._get_message(source(thread_id="42"), 81)
