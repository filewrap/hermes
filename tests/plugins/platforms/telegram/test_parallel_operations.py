"""Exercise the real operations -> parallel helper -> Telethon MTProto iterator path."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone, UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types
from telethon.client.downloads import DownloadMethods

from plugins.platforms.telegram.client_operations import TelegramClientOperations
from plugins.platforms.telegram.parallel_download import PARALLEL_PARTS, PART_SIZE


class PartClient(DownloadMethods):
    def __init__(self, size):
        self.size = size
        self.session = SimpleNamespace(dc_id=2)
        self._sender = object()
        self._log = defaultdict(lambda: logging.getLogger(__name__))
        self.offsets = []
        self.active = 0
        self.maximum_active = 0
        self.barrier = asyncio.Event()
        self.expire = False
        self.get_messages = AsyncMock(return_value=[])

    async def _call(self, sender, request):
        assert sender is self._sender
        assert isinstance(request, functions.upload.GetFileRequest)
        self.offsets.append(request.offset)
        if self.expire:
            raise errors.FileReferenceExpiredError(request)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active >= PARALLEL_PARTS:
            self.barrier.set()
        try:
            await asyncio.wait_for(self.barrier.wait(), timeout=5)
            # Replies intentionally arrive in reverse order within the window.
            for _ in range(PARALLEL_PARTS - (request.offset // PART_SIZE) % PARALLEL_PARTS):
                await asyncio.sleep(0)
            length = min(request.limit, self.size - request.offset)
            return types.upload.File(type=types.storage.FileUnknown(), mtime=0,
                                     bytes=bytes([request.offset // PART_SIZE % 251]) * length)
        finally:
            self.active -= 1


def source(profile="alpha"):
    return SimpleNamespace(chat_id="-1000000000321", user_id="7", thread_id=None,
                           is_bot=False, is_guest=False, profile=profile)


def message(size):
    document = types.Document(id=702, access_hash=1, file_reference=b"ref",
                              date=datetime.now(UTC), mime_type="application/octet-stream",
                              size=size, dc_id=2,
                              attributes=[types.DocumentAttributeFilename("application.apk")])
    return SimpleNamespace(id=81, chat_id=source().chat_id, sender_id=12, date=None,
                           message="Original APK caption", media=types.MessageMediaDocument(document=document),
                           file=None)


def adapter(home, client):
    return SimpleNamespace(_client=client, _authorized=lambda _: True,
                           _settings_home=lambda src: home / src.profile,
                           _input_peer_for=AsyncMock(return_value=object()),
                           _message_thread_id=lambda _: None)


@pytest.mark.asyncio
async def test_real_telethon_parts_pipeline_preserves_order_metadata_and_profile_scope(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="plugins.platforms.telegram.client_operations")
    size = 6 * PART_SIZE + 37
    client = PartClient(size)
    ops = TelegramClientOperations(adapter(tmp_path, client))
    media = message(size)
    ops.observe(source(), media)
    progress = []

    async def report(current, total):
        progress.append((current, total))

    for profile in ("alpha", "beta", "alpha"):
        result = await ops.request(source(profile), "files_download", {"message_id": 81},
                                   progress_callback=report)
        if profile == "beta":
            assert not result["ok"]
            continue
        assert result["ok"], result
        data = result["data"]
        path = Path(data["cached_path"])
        assert path.is_relative_to(tmp_path / profile)
        assert path.read_bytes() == b"".join(bytes([i % 251]) * min(PART_SIZE, size - i * PART_SIZE)
                                             for i in range(7))
        assert data["text"] == "Original APK caption"
        assert data["file_name"] == "application.apk"
        assert data["mime_type"] == "application/octet-stream"
        assert data["downloaded_bytes"] == size
        assert "download_limits" not in data
        assert data["elapsed_seconds"] > 0
        assert data["average_bytes_per_second"] == pytest.approx(size / data["elapsed_seconds"])
        diagnostics = data["download_diagnostics"]
        assert diagnostics["strategy"] == "rolling_mtproto_window"
        assert diagnostics["write_order"] == "completion_offset"
        assert diagnostics["sender_mode"] == "shared_per_dc"
        assert diagnostics["crypto_backend"] in {"cryptg", "libssl", "pyaes"}
        assert diagnostics["part_size_bytes"] == PART_SIZE
        assert diagnostics["window_parts"] == PARALLEL_PARTS
        assert diagnostics["retry_count"] == 0
        assert diagnostics["disk_write_seconds"] >= 0
        assert diagnostics["part_request_seconds_sum"] > 0
        assert diagnostics["scope_check_count"] > 0
        assert diagnostics["scope_check_seconds_sum"] >= 0
        arrivals = diagnostics["arrival_throughput"]
        assert arrivals["received_bytes"] == size
        assert arrivals["received_mb"] == size / 1_000_000
        assert sum(bucket["received_mb"] for bucket in arrivals["intervals"]) == pytest.approx(size / 1_000_000)
        assert arrivals["first_arrival_seconds"] <= arrivals["last_arrival_seconds"] <= arrivals["elapsed_seconds"]
        assert arrivals["coverage"] == "parallel_download"
        assert all(value not in repr(diagnostics) for value in (
            "application.apk", "Original APK caption", source().chat_id, str(path)))
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert progress[-1] == (size, size)
    assert client.maximum_active == PARALLEL_PARTS
    assert client.offsets == list(range(0, size, PART_SIZE)) * 2
    assert not list(tmp_path.rglob(".telegram-download-*"))
    completion_logs = [record.message for record in caplog.records
                       if record.message.startswith("Telegram media transfer complete:")]
    assert len(completion_logs) == 2
    assert all("arrival_throughput" in log and "received_mb" in log for log in completion_logs)


@pytest.mark.asyncio
async def test_parallel_reference_refresh_rejects_different_chat_and_cleans_partials(tmp_path):
    size = 4 * PART_SIZE
    client = PartClient(size)
    client.expire = True
    wrong_chat = message(size)
    wrong_chat.chat_id = "-1000000000999"
    client.get_messages.return_value = [wrong_chat]
    owner_adapter = adapter(tmp_path, client)
    ops = TelegramClientOperations(owner_adapter)
    ops.observe(source(), message(size))
    result = await ops.request(source(), "files_download", {"message_id": 81})
    assert result["error"]["code"] == "DOWNLOAD_SCOPE_CHANGED"
    assert client.get_messages.await_count >= 1
    assert all(call.kwargs == {"ids": [81]} for call in client.get_messages.await_args_list)
    assert not list(tmp_path.rglob(".telegram-download-*"))
    assert not list(tmp_path.rglob("*.apk"))
    assert client.active == 0


@pytest.mark.asyncio
async def test_parallel_cancel_removes_temporary_file_and_stops_all_requests(tmp_path):
    size = 4 * PART_SIZE
    client = PartClient(size)
    entered = asyncio.Event()

    async def blocked(_sender, _request):
        client.active += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            client.active -= 1

    client._call = blocked
    ops = TelegramClientOperations(adapter(tmp_path, client))
    ops.observe(source(), message(size))
    task = asyncio.create_task(ops.request(source(), "files_download", {"message_id": 81}))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.active == 0
    assert not list(tmp_path.rglob(".telegram-download-*"))
    assert not list(tmp_path.rglob("*.apk"))


@pytest.mark.asyncio
async def test_announced_backoff_outlives_idle_timeout_without_losing_cancellation_checks(tmp_path, monkeypatch):
    from plugins.platforms.telegram import client_operations as operations_module
    monkeypatch.setattr(operations_module, "DOWNLOAD_IDLE_TIMEOUT", 0.1)
    size = PARALLEL_PARTS * PART_SIZE
    client = PartClient(size)
    native_call = client._call
    first = True

    async def transient(sender, request):
        nonlocal first
        if first:
            first = False
            raise ConnectionError("retry this part")
        return await native_call(sender, request)

    client._call = transient
    ops = TelegramClientOperations(adapter(tmp_path, client))
    ops.observe(source(), message(size))
    result = await ops.request(source(), "files_download", {"message_id": 81})
    assert result["ok"], result
    assert result["data"]["downloaded_bytes"] == size
    assert not list(tmp_path.rglob(".telegram-download-*"))
