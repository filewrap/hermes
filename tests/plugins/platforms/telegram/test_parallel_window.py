"""Prove bounded completion-driven scheduling without live-network timing assertions."""
import asyncio
from types import SimpleNamespace

import pytest
from telethon import types

from plugins.platforms.telegram import parallel_download as parallel


PART = parallel.PART_SIZE


class Stream:
    def __init__(self, response, closed):
        self.response = response
        self.closed = closed

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.response()

    async def close(self):
        self.closed.set()


def message(size):
    return SimpleNamespace(media=SimpleNamespace(document=types.Document(
        id=1, access_hash=2, file_reference=b"ref", date=1,
        mime_type="application/octet-stream", size=size, dc_id=2, attributes=[])))


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_index", [0, 1])
async def test_completed_parts_refill_connections_while_earlier_part_is_blocked(tmp_path, slow_index):
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    followers_written = asyncio.Event()
    count = 2 * parallel.PARALLEL_PARTS + 1
    size = (count - 1) * PART + 37
    slow_offset = slow_index * PART
    offsets = []
    closed = []
    progress_bytes = []
    active = peak = 0

    class Client:
        def iter_download(self, _document, *, offset, **_kwargs):
            offsets.append(offset)
            done = asyncio.Event()
            closed.append(done)

            async def response():
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    if offset == slow_offset:
                        slow_started.set()
                        await release_slow.wait()
                    else:
                        await slow_started.wait()
                    return bytes([offset // PART]) * min(PART, size - offset)
                finally:
                    active -= 1

            return Stream(response, done)

    async def progress(current, total):
        assert total == size
        progress_bytes.append(current)
        if current == size - PART:
            followers_written.set()
        await asyncio.sleep(0)

    output = tmp_path / "completion-driven"
    task = asyncio.create_task(parallel.download_document(
        Client(), message(size), str(output), size,
        progress_callback=progress, check_scope=lambda: None,
        refresh_document=lambda: pytest.fail("unexpected refresh"),
        slots=asyncio.Semaphore(parallel.PARALLEL_PARTS)))
    try:
        # Event-driven, not a speed assertion: the ordered implementation stalls
        # here with idle connections until the delayed head is released.
        await asyncio.wait_for(followers_written.wait(), timeout=5)
        assert not release_slow.is_set()
        assert offsets == list(range(0, size, PART))
        assert progress_bytes[-1] == size - PART
        assert not task.done()
        assert peak <= parallel.PARALLEL_PARTS
        assert active == 1
    finally:
        release_slow.set()
        await task
    assert progress_bytes == sorted(set(progress_bytes))
    assert progress_bytes[-1] == size
    assert output.read_bytes() == b"".join(
        bytes([i]) * min(PART, size - i * PART) for i in range(count))
    assert all(event.is_set() for event in closed)


def test_cryptg_is_compatible_with_telethon_libssl_and_public_aes_selection():
    cryptg = pytest.importorskip("cryptg")
    from telethon.crypto import AES, aes
    if not aes.libssl:
        pytest.skip("OpenSSL AES-IGE backend is not available for interoperability check")
    plaintext = bytes(range(256)) * 4
    key = bytes(range(32))
    iv = bytes(reversed(range(32)))
    encrypted = cryptg.encrypt_ige(plaintext, key, iv)
    assert aes.libssl.decrypt_ige(encrypted, key, iv) == plaintext
    assert cryptg.decrypt_ige(aes.libssl.encrypt_ige(plaintext, key, iv), key, iv) == plaintext
    assert AES.decrypt_ige(encrypted, key, iv) == plaintext


@pytest.mark.asyncio
async def test_blocked_progress_keeps_completed_payloads_inside_five_part_budget(tmp_path, monkeypatch):
    writer_paused = asyncio.Event()
    release_writer = asyncio.Event()
    refill_completed = asyncio.Event()
    live = peak = requests = 0

    class Payload(bytes):
        # Exercise the production task/result lifetime, not only active requests.
        def __new__(cls, value):
            nonlocal live, peak
            payload = super().__new__(cls, value)
            live += 1
            peak = max(peak, live)
            return payload

        def __del__(self):
            nonlocal live
            live -= 1

    monkeypatch.setattr(parallel, "bytes", Payload, raising=False)

    class Client:
        def iter_download(self, _document, *, offset, **_kwargs):
            async def response():
                nonlocal requests
                requests += 1
                if requests == parallel.PARALLEL_PARTS + 1:
                    refill_completed.set()
                return b"x" * PART
            return Stream(response, asyncio.Event())

    async def progress(*_args):
        writer_paused.set()
        await release_writer.wait()

    size = 20 * PART
    task = asyncio.create_task(parallel.download_document(
        Client(), message(size), str(tmp_path / "memory-bound"), size,
        progress_callback=progress, check_scope=lambda: None,
        refresh_document=lambda: pytest.fail("unexpected refresh"),
        slots=asyncio.Semaphore(parallel.PARALLEL_PARTS)))
    try:
        await asyncio.wait_for(writer_paused.wait(), timeout=5)
        await asyncio.wait_for(refill_completed.wait(), timeout=5)
        assert requests == parallel.PARALLEL_PARTS + 1
        assert live == parallel.PARALLEL_PARTS
        assert live * PART <= 5 * 512 * 1024
        assert peak <= parallel.PARALLEL_PARTS
    finally:
        release_writer.set()
        await task
    assert peak <= parallel.PARALLEL_PARTS
    assert live == 0
    assert (tmp_path / "memory-bound").read_bytes() == b"x" * size
