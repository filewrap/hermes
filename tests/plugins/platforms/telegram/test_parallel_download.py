"""Deterministic tests for bounded parallel MTProto document downloads."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import errors, types
from telethon.client.downloads import DownloadMethods, _CdnRedirect

from plugins.platforms.telegram import parallel_download as parallel
from plugins.platforms.telegram.client_operations import TelegramClientOperations


PART = parallel.PART_SIZE


def document(size: int, *, file_reference: bytes = b"reference", doc_id: int = 77) -> types.Document:
    """A genuine Telethon Document, so public iterator location conversion is exercised."""
    return types.Document(
        id=doc_id, access_hash=901, file_reference=file_reference, date=1,
        mime_type="application/octet-stream", size=size, dc_id=2, attributes=[],
    )


def message(doc: types.Document, message_id: int = 31):
    return SimpleNamespace(id=message_id, media=SimpleNamespace(document=doc), message="file")


class FakeStream:
    """One-item async iterator recording close even on cancellation/failure."""
    def __init__(self, response, closed=None):
        self.response = response
        self.closed = closed
        self.used = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.used:
            raise StopAsyncIteration
        self.used = True
        if isinstance(self.response, BaseException):
            raise self.response
        if asyncio.iscoroutine(self.response):
            return await self.response
        return self.response

    async def close(self):
        if self.closed is not None:
            self.closed.set()


class RequestClient:
    """Fake _call transport behind Telethon's real DownloadMethods.iter_download."""
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = []
        self._sender = object()
        self.session = SimpleNamespace(dc_id=2)
        self._log = {"telethon.client.downloads": SimpleNamespace(info=lambda *_a, **_k: None)}
        self._iter_download = lambda *args, **kwargs: DownloadMethods._iter_download(self, *args, **kwargs)

    def iter_download(self, file, **kwargs):
        return DownloadMethods.iter_download(self, file, **kwargs)

    async def _call(self, sender, request):
        from telethon.tl.functions.upload import GetFileRequest
        assert sender is self._sender
        assert isinstance(request, GetFileRequest)
        self.calls.append((request.offset, request.limit))
        return types.upload.File(type="unknown", mtime=0,
                                 bytes=self.payload[request.offset:request.offset + request.limit])


@pytest.mark.asyncio
async def test_uses_telethon_145_public_iterator_and_upload_get_file_requests(tmp_path):
    # Exercise the pinned Telethon iterator rather than imitating its offset math.
    assert __import__("telethon").__version__ == "1.45.0"
    payload = bytes(i * 13 % 251 for i in range(2 * PART))
    client = RequestClient(payload)
    progress = []

    async def report(current, total):
        progress.append((current, total))

    await parallel.download_document(client, message(document(len(payload))), str(tmp_path / "out"),
                                     len(payload), progress_callback=report, check_scope=lambda: None,
                                     refresh_document=lambda: pytest.fail("unexpected refresh"),
                                     slots=asyncio.Semaphore(4))

    output = (tmp_path / "out").read_bytes()
    assert output == payload
    assert hashlib.sha256(output).digest() == hashlib.sha256(payload).digest()
    assert sorted(client.calls) == [(0, PART), (PART, PART)]
    assert progress[-1] == (len(payload), len(payload))


@pytest.mark.asyncio
async def test_parts_overlap_write_in_offset_order_and_preserve_short_final_part(tmp_path):
    size = 2 * PART + 37
    expected = [b"A" * PART, b"B" * PART, b"C" * 37]
    started = []
    all_started = asyncio.Event()
    release = {index: asyncio.Event() for index in range(3)}
    finished = []
    calls = []

    class Client:
        def iter_download(self, doc, *, offset, limit, request_size, chunk_size, file_size):
            calls.append((doc, offset, limit, request_size, chunk_size, file_size))
            index = offset // PART
            async def response():
                started.append(index)
                if len(started) == 3:
                    all_started.set()
                await all_started.wait()
                await release[index].wait()
                finished.append(index)
                return expected[index]
            return FakeStream(response())

    client = Client()
    task = asyncio.create_task(parallel.download_document(
        client, message(document(size)), str(tmp_path / "ordered"), size,
        progress_callback=lambda *_: asyncio.sleep(0), check_scope=lambda: None,
        refresh_document=lambda: pytest.fail("unexpected refresh"), slots=asyncio.Semaphore(4)))
    # The response barrier cannot open until every request is running concurrently.
    await asyncio.wait_for(all_started.wait(), timeout=10)
    assert len(set(started)) == 3
    # Release reverse offsets; the file must still be assembled in ascending offset order.
    for index in (2, 1, 0):
        release[index].set()
        while index not in finished:
            await asyncio.sleep(0)
    await task

    assert finished == [2, 1, 0]
    assert (tmp_path / "ordered").read_bytes() == b"".join(expected)
    assert sorted(call[1] for call in calls) == [0, PART, 2 * PART]
    assert all(call[2:] == (1, PART, PART, size) for call in calls)


@pytest.mark.asyncio
async def test_transient_and_floodwait_retries_use_exponential_or_server_delay(tmp_path, monkeypatch):
    delays = []

    async def record_sleep(delay):
        delays.append(delay)

    async def no_progress_sleep(*_args):
        pass

    monkeypatch.setattr(parallel.asyncio, "sleep", record_sleep)
    outcomes = {0: [TimeoutError("socket"), b"x" * PART],
                PART: [errors.FloodWaitError(request=None, capture=3), b"y" * PART]}
    attempts = {0: 0, PART: 0}

    class Client:
        def iter_download(self, _doc, *, offset, **_kwargs):
            attempts[offset] += 1
            return FakeStream(outcomes[offset].pop(0))

    size = 2 * PART
    await parallel.download_document(
        Client(), message(document(size)), str(tmp_path / "retry"), size,
        progress_callback=no_progress_sleep, check_scope=lambda: None,
        refresh_document=lambda: pytest.fail("unexpected refresh"), slots=asyncio.Semaphore(4))
    assert attempts == {0: 2, PART: 2}
    assert sorted(delays) == [0.5, 3.0]
    assert (tmp_path / "retry").read_bytes() == b"x" * PART + b"y" * PART


@pytest.mark.asyncio
async def test_transient_retry_exhaustion_raises_original_error_and_closes_each_stream(tmp_path, monkeypatch):
    async def no_sleep(_delay):
        pass

    async def no_progress_sleep(*_args):
        pass

    monkeypatch.setattr(parallel.asyncio, "sleep", no_sleep)
    closed = []
    failure = ConnectionError("disconnected")
    attempts = 0

    class Client:
        def iter_download(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            event = asyncio.Event()
            closed.append(event)
            return FakeStream(failure, event)

    with pytest.raises(ConnectionError, match="disconnected"):
        await parallel.download_document(
            Client(), message(document(PART)), str(tmp_path / "failed"), PART,
            progress_callback=no_progress_sleep, check_scope=lambda: None,
            refresh_document=lambda: pytest.fail("unexpected refresh"), slots=asyncio.Semaphore(4))
    assert attempts == parallel.PART_ATTEMPTS
    assert len(closed) == parallel.PART_ATTEMPTS and all(event.is_set() for event in closed)


@pytest.mark.asyncio
async def test_expired_reference_refreshes_same_document_once_for_concurrent_parts(tmp_path):
    original = document(2 * PART, file_reference=b"expired")
    refreshed = document(2 * PART, file_reference=b"fresh")
    calls = []
    refreshes = 0

    async def refresh():
        nonlocal refreshes
        refreshes += 1
        await asyncio.sleep(0)
        return refreshed

    class Client:
        def iter_download(self, doc, *, offset, **_kwargs):
            calls.append((doc, offset))
            if doc is original:
                return FakeStream(errors.FileReferenceExpiredError(request=None))
            return FakeStream(bytes([65 + offset // PART]) * PART)

    await parallel.download_document(
        Client(), message(original), str(tmp_path / "refresh"), 2 * PART,
        progress_callback=lambda *_: asyncio.sleep(0), check_scope=lambda: None,
        refresh_document=refresh, slots=asyncio.Semaphore(4))
    assert refreshes == 1
    assert sorted(offset for doc, offset in calls if doc is original) == [0, PART]
    assert sorted(offset for doc, offset in calls if doc is refreshed) == [0, PART]
    assert (tmp_path / "refresh").read_bytes() == b"A" * PART + b"B" * PART


@pytest.mark.asyncio
async def test_shared_semaphore_bounds_inflight_requests_across_downloads(tmp_path):
    shared = asyncio.Semaphore(2)
    both_active = asyncio.Event()
    active = peak = 0
    in_flight = 0

    async def no_progress_sleep(*_args):
        pass

    class Client:
        def iter_download(self, _doc, *, offset, **_kwargs):
            async def response():
                nonlocal active, peak, in_flight
                active += 1
                in_flight += 1
                peak = max(peak, active)
                if active == 2:
                    both_active.set()
                await both_active.wait()
                active -= 1
                return bytes([65 + offset // PART]) * PART
            return FakeStream(response())

    async def run(index):
        await parallel.download_document(
            Client(), message(document(2 * PART, doc_id=77 + index), 31 + index),
            str(tmp_path / f"shared-{index}"), 2 * PART,
            progress_callback=no_progress_sleep, check_scope=lambda: None,
            refresh_document=lambda: pytest.fail("unexpected refresh"), slots=shared)

    await asyncio.gather(run(0), run(1))
    assert peak == 2
    assert in_flight == 4
    for index in range(2):
        assert (tmp_path / f"shared-{index}").stat().st_size == 2 * PART


@pytest.mark.asyncio
async def test_failure_cancels_and_closes_sibling_iterators(tmp_path):
    sibling_started = asyncio.Event()
    closed = {0: asyncio.Event(), PART: asyncio.Event()}

    class Client:
        def iter_download(self, _doc, *, offset, **_kwargs):
            if offset == 0:
                async def fail():
                    await sibling_started.wait()
                    raise ValueError("bad part")
                return FakeStream(fail(), closed[offset])
            async def blocked():
                sibling_started.set()
                await asyncio.Event().wait()
            return FakeStream(blocked(), closed[offset])

    with pytest.raises(ValueError, match="bad part"):
        await parallel.download_document(
            Client(), message(document(2 * PART)), str(tmp_path / "cancel-sibling"), 2 * PART,
            progress_callback=lambda *_: asyncio.sleep(0), check_scope=lambda: None,
            refresh_document=lambda: pytest.fail("unexpected refresh"), slots=asyncio.Semaphore(4))
    assert all(event.is_set() for event in closed.values())


@pytest.mark.asyncio
async def test_external_cancellation_closes_iterator_and_propagates(tmp_path):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Client:
        def iter_download(self, *_args, **_kwargs):
            async def blocked():
                entered.set()
                await asyncio.Event().wait()
            return FakeStream(blocked(), closed)

    task = asyncio.create_task(parallel.download_document(
        Client(), message(document(PART)), str(tmp_path / "cancel"), PART,
        progress_callback=lambda *_: asyncio.sleep(0), check_scope=lambda: None,
        refresh_document=lambda: pytest.fail("unexpected refresh"), slots=asyncio.Semaphore(4)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_cdn_redirect_cancels_parallel_parts_then_uses_native_writer(tmp_path):
    sibling_started = asyncio.Event()
    closed = {0: asyncio.Event(), PART: asyncio.Event()}
    fallback_calls = []
    payload = b"native-fallback"

    class Client:
        def iter_download(self, _doc, *, offset, **_kwargs):
            if offset == 0:
                async def redirect():
                    await sibling_started.wait()
                    raise _CdnRedirect()
                return FakeStream(redirect(), closed[offset])
            async def blocked():
                sibling_started.set()
                await asyncio.Event().wait()
            return FakeStream(blocked(), closed[offset])

        async def download_media(self, msg, *, file, progress_callback):
            fallback_calls.append(msg)
            Path(file).write_bytes(payload)
            await progress_callback(len(payload), len(payload))
            return file

    msg = message(document(2 * PART))
    progress = []
    async def report(current, total):
        progress.append((current, total))
    stats = await parallel.download_document(
        Client(), msg, str(tmp_path / "cdn"), 2 * PART,
        progress_callback=report, check_scope=lambda: None,
        refresh_document=lambda: pytest.fail("unexpected refresh"), slots=asyncio.Semaphore(4))
    assert stats["arrival_throughput"]["coverage"] == "parallel_attempt_only_excludes_native_cdn_fallback"
    assert stats["arrival_throughput"]["received_bytes"] == 0
    assert fallback_calls == [msg]
    assert all(event.is_set() for event in closed.values())
    assert (tmp_path / "cdn").read_bytes() == payload
    assert progress[-1] == (len(payload), len(payload))


@pytest.mark.asyncio
async def test_telegram_operations_selects_real_large_documents_for_parallel_path(tmp_path):
    size = PART * 2
    doc = document(size, doc_id=771)
    msg = SimpleNamespace(id=81, message="large", date=None, sender_id=7,
                          media=SimpleNamespace(document=doc), file=None)
    source = SimpleNamespace(platform="telegram", user_id="7", chat_id="-100001",
                             thread_id=None, profile="test", is_bot=False,
                             is_guest=False, chat_type="group", message_id="81")

    class Client:
        def __init__(self):
            self.calls = []
        def iter_download(self, _doc, *, offset, **_kwargs):
            self.calls.append(offset)
            return FakeStream(bytes([65 + offset // PART]) * PART)
        async def download_media(self, *_args, **_kwargs):
            pytest.fail("large genuine Document should use parallel helper")

    class Adapter:
        def __init__(self):
            self.homes = {"test": tmp_path}
            self._client = Client()
        def _authorized(self, _source): return True
        def _is_owner_user(self, user_id, _source): return str(user_id) == "7"
        def _settings_home(self, src): return self.homes[src.profile]
        async def _input_peer_for(self, _chat_id): return object()
        @staticmethod
        def _message_thread_id(_message): return None

    adapter = Adapter()
    ops = TelegramClientOperations(adapter)
    ops.observe(source, msg)
    result = await ops.request(source, "files_download", {"message_id": msg.id})
    assert result["ok"], result
    output = Path(result["data"]["cached_path"])
    assert output.read_bytes() == b"A" * PART + b"B" * PART
    assert sorted(adapter._client.calls) == [0, PART]


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_size", [PART - 1, PART + 1])
async def test_wrong_sized_part_is_rejected_and_iterator_closed(tmp_path, returned_size):
    closed = asyncio.Event()

    class Client:
        def iter_download(self, *_args, **_kwargs):
            return FakeStream(b"x" * returned_size, closed)

    with pytest.raises(parallel.IncompletePartError):
        await parallel.download_document(
            Client(), message(document(PART)), str(tmp_path / "incomplete"), PART,
            progress_callback=lambda *_: asyncio.sleep(0), check_scope=lambda: None,
            refresh_document=lambda: pytest.fail("unexpected refresh"), slots=asyncio.Semaphore(4))
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_backoff", [False, True])
async def test_flood_backoff_frees_budget_for_waiting_download_and_remains_cancellable(
        tmp_path, monkeypatch, cancel_backoff):
    capacity = parallel.PARALLEL_PARTS
    slots = asyncio.Semaphore(capacity)
    backing_off = asyncio.Event()
    release_backoff = asyncio.Event()
    followers_started = asyncio.Event()
    release_followers = asyncio.Event()
    delays = []
    attempts = []
    closed = []
    follower_count = 0
    original_sleep = asyncio.sleep

    async def sleep(delay):
        if delay == 3.0:
            delays.append(delay)
            backing_off.set()
            await release_backoff.wait()
        else:
            await original_sleep(delay)

    monkeypatch.setattr(parallel.asyncio, "sleep", sleep)

    class Client:
        def iter_download(self, doc, *, offset, **_kwargs):
            event = asyncio.Event()
            closed.append(event)

            async def response():
                nonlocal follower_count
                if doc.id == 77:
                    if offset == 0:
                        attempts.append(offset)
                        if len(attempts) == 1:
                            raise errors.FloodWaitError(request=None, capture=3)
                    else:
                        follower_count += 1
                        if follower_count == capacity - 1:
                            followers_started.set()
                        await release_followers.wait()
                return b"x" * PART

            return FakeStream(response(), event)

    async def run(name, doc_id, count):
        return await parallel.download_document(
            Client(), message(document(count * PART, doc_id=doc_id)),
            str(tmp_path / name), count * PART,
            progress_callback=lambda *_: original_sleep(0), check_scope=lambda: None,
            refresh_document=lambda: pytest.fail("unexpected refresh"), slots=slots)

    first = asyncio.create_task(run("backoff", 77, capacity))
    second = None
    try:
        await asyncio.wait_for(backing_off.wait(), 10)
        await asyncio.wait_for(followers_started.wait(), 10)
        # Five live requests plus one sleeping retry leave one shared slot free.
        # The retry must not issue again before the server-delay gate is released.
        second = asyncio.create_task(run("waiting", 78, 1))
        await asyncio.wait_for(asyncio.shield(second), 10)
        assert attempts == [0]
        assert delays == [3.0]
        assert (tmp_path / "waiting").read_bytes() == b"x" * PART
        if cancel_backoff:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            release_backoff.set()
            release_followers.set()
            result = await asyncio.wait_for(asyncio.shield(first), 10)
            assert attempts == [0, 0]
            assert result["retry_count"] == 1
            assert result["requested_retry_wait_seconds"] == 3.0
            assert (tmp_path / "backoff").read_bytes() == b"x" * (capacity * PART)
    finally:
        release_backoff.set()
        release_followers.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (first, second) if t is not None), return_exceptions=True)
    assert all(event.is_set() for event in closed)
    # All slots must remain usable after either successful retry or cancellation.
    await asyncio.wait_for(run("after-backoff", 79, capacity), 10)
    assert (tmp_path / "after-backoff").stat().st_size == capacity * PART
