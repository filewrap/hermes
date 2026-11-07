"""Offline proof of pinned Telethon sender isolation, bounds and lifecycle.

Only the socket/sender transport is replaced. TelegramClient._call, document
conversion and DownloadMethods' real range/migration/iterator cleanup execute.
"""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient, errors, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession

from plugins.platforms.telegram import download_connections as connections
from plugins.platforms.telegram import parallel_download as parallel

PART = parallel.PART_SIZE


def test_five_way_trial_budget_is_shared_and_higher_capacities_are_rejected():
    # Five is an owner-requested live trial, not a measured speed improvement.
    # Keep the sender/request/window limits tied together; six regressed live.
    assert connections.MAX_DOWNLOAD_CONNECTIONS == 5
    assert parallel.PARALLEL_PARTS == connections.MAX_DOWNLOAD_CONNECTIONS
    assert connections.DownloadConnectionPool().capacity == parallel.PARALLEL_PARTS
    assert parallel.PART_SIZE == 512 * 1024
    assert parallel.PARALLEL_PARTS * parallel.PART_SIZE == 5 * 512 * 1024
    for capacity in (0, 6, 7):
        with pytest.raises(ValueError):
            connections.DownloadConnectionPool(capacity)


@pytest.fixture
def transport(monkeypatch):
    made = []
    requests = []
    gate = asyncio.Event()
    reached = asyncio.Event()
    state = SimpleNamespace(active=0, peak=0, target=4, outcome="success", failed=False, migrations=0)

    class Sender:
        def __init__(self, auth_key, **kwargs):
            self.auth_key = auth_key
            self.kwargs = kwargs
            self.connected = False
            self.closed = 0
            self.initialized = False
            self.pending = set()
            made.append(self)

        async def connect(self, connection):
            self.connection = connection
            self.connected = True
            return True

        def is_connected(self):
            return self.connected

        async def disconnect(self):
            self.connected = False
            self.closed += 1
            for task in self.pending:
                task.cancel()
            await asyncio.gather(*self.pending, return_exceptions=True)

        def send(self, request, ordered=False):
            async def reply():
                current_request = request
                if isinstance(current_request, functions.InvokeWithLayerRequest):
                    assert isinstance(current_request.query, functions.InvokeWithoutUpdatesRequest)
                    assert isinstance(current_request.query.query, functions.InitConnectionRequest)
                    assert isinstance(current_request.query.query.query, functions.help.GetConfigRequest)
                    self.initialized = True
                    return SimpleNamespace()
                if self is not getattr(self, "borrowed_identity", None):
                    assert isinstance(current_request, functions.InvokeWithoutUpdatesRequest)
                    current_request = current_request.query
                assert isinstance(current_request, functions.upload.GetFileRequest)
                assert self.initialized
                # Native iterators advance this same request object's offset
                # after receiving a part. Record what was actually sent.
                requests.append((self, copy.copy(current_request)))
                state.active += 1
                state.peak = max(state.peak, state.active)
                if state.active >= state.target:
                    reached.set()
                try:
                    await gate.wait()
                    if state.outcome == "failure":
                        raise ValueError("bad media")
                    if state.outcome == "cdn":
                        from telethon.client.downloads import _CdnRedirect
                        raise _CdnRedirect()
                    if state.outcome == "expired" and not state.failed:
                        state.failed = True
                        raise errors.FileReferenceExpiredError(request)
                    if state.outcome == "migrate" and not state.failed:
                        state.failed = True
                        raise errors.FileMigrateError(request, capture=3)
                    if state.outcome == "migrate_twice" and state.migrations < 2:
                        state.migrations += 1
                        raise errors.FileMigrateError(request, capture=2 + state.migrations)
                    length = min(current_request.limit, state.size - current_request.offset)
                    return types.upload.File(type=types.storage.FileUnknown(), mtime=0,
                                             bytes=bytes([current_request.offset // PART % 251]) * length)
                finally:
                    state.active -= 1
            task = asyncio.create_task(reply())
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)
            return task

    def client():
        session = MemorySession()
        session.set_dc(2, "127.0.0.1", 443)
        session.auth_key = AuthKey(bytes(range(256)))
        result = TelegramClient(session, 1, "offline-test", request_retries=0)
        # Never call TelegramClient.connect: all connection methods are inert.
        primary = Sender(session.auth_key)
        primary.initialized = True
        primary.connected = True
        result._sender = primary
        result._get_dc = AsyncMock(side_effect=lambda dc_id: SimpleNamespace(
            id=dc_id, ip_address="127.0.0.1", port=443))
        result._connection = lambda *args, **kwargs: (args, kwargs)
        remote = Sender(AuthKey(bytes(reversed(range(256)))))
        remote.initialized = True
        remote.connected = True
        remote.borrowed_identity = remote
        remote.dc_id = 3
        result._borrow_exported_sender = AsyncMock(return_value=remote)
        result._return_exported_sender = AsyncMock()
        return result

    monkeypatch.setattr(connections, "MTProtoSender", Sender)
    return SimpleNamespace(client=client, made=made, requests=requests, state=state,
                           gate=gate, reached=reached, sender_type=Sender)


def media(size, dc_id=2, ref=b"ref"):
    return SimpleNamespace(media=types.MessageMediaDocument(document=types.Document(
        id=77, access_hash=3, file_reference=ref, date=1, mime_type="application/octet-stream",
        size=size, dc_id=dc_id, attributes=[])))


@pytest.mark.asyncio
@pytest.mark.parametrize("dc_id", [2, 3])
@pytest.mark.parametrize("capacity", [2, 4, 5])
async def test_real_iterator_independent_connections_are_bounded_and_reused_across_downloads(
        tmp_path, transport, dc_id, capacity):
    t = transport
    client = t.client()
    full_parts = 2 * max(capacity, parallel.PARALLEL_PARTS)
    size = full_parts * PART + 37
    t.state.size = size
    t.state.target = capacity
    pool = connections.DownloadConnectionPool(capacity)
    slots = asyncio.Semaphore(capacity)
    original_query = client._init_request.query
    outputs = [tmp_path / f"out-{i}" for i in range(2)]

    async def run(index, path):
        message = media(size, dc_id)
        message.media.document.id = 77 + index
        return await parallel.download_document(
            client, message, str(path), size,
            progress_callback=lambda *_: asyncio.sleep(0), check_scope=lambda: None,
            refresh_document=lambda: pytest.fail("unexpected refresh"), slots=slots,
            connections=pool)

    tasks = [asyncio.create_task(run(index, path)) for index, path in enumerate(outputs)]
    try:
        await asyncio.wait_for(t.reached.wait(), 10)
        active_senders = {sender for sender, _ in t.requests}
        assert len(active_senders) == capacity
        assert client._sender not in active_senders
        assert all(sender.connection is not None for sender in active_senders)
        assert all(sender.kwargs["updates_queue"] is client._updates_queue for sender in active_senders)
        assert t.state.peak == capacity
        assert len([s for s in t.made if s not in (client._sender,
                    client._borrow_exported_sender.return_value)]) == capacity
    finally:
        t.gate.set()
        results = await asyncio.gather(*tasks)
    expected = b"".join(bytes([i]) * min(PART, size - i * PART) for i in range(full_parts + 1))
    assert all(path.read_bytes() == expected for path in outputs)
    assert all(r["sender_mode"] == "independent_connections" for r in results)
    assert all(r["peak_active_part_requests"] <= capacity for r in results)
    assert t.state.peak == capacity
    starts = [(request.location.id, request.offset) for _, request in t.requests]
    # Queued work from the second file must not wait for the first whole file.
    assert starts.index((78, 0)) < starts.index((77, full_parts * PART))
    used = {sender for sender, _ in t.requests}
    assert all(sender.closed == 1 and not sender.pending for sender in used)
    assert client._sender.closed == 0
    assert client._init_request.query is original_query
    assert client.session.dc_id == 2
    assert client.session.auth_key is client._sender.auth_key
    if dc_id == 3:
        assert client._borrow_exported_sender.await_count > 0
        assert client._return_exported_sender.await_count == client._borrow_exported_sender.await_count
        assert all(sender.auth_key.key == client._borrow_exported_sender.return_value.auth_key.key
                   for sender in used)
    else:
        client._borrow_exported_sender.assert_not_awaited()
        assert all(sender.auth_key.key == client._sender.auth_key.key for sender in used)
    assert all(sender.auth_key is not client._sender.auth_key for sender in used)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancel", "failure", "expired", "migrate", "migrate_twice", "cdn", "setup_cancel"])
async def test_dedicated_connections_close_on_every_exit_without_touching_primary(
        tmp_path, transport, outcome):
    t = transport
    client = t.client()
    capacity = parallel.PARALLEL_PARTS
    size = capacity * PART
    t.state.target = capacity
    t.state.size = size
    t.state.outcome = outcome
    refresh = AsyncMock(return_value=media(size, ref=b"fresh").media.document)
    fallback = AsyncMock()
    client.download_media = fallback
    pool = connections.DownloadConnectionPool()
    if outcome == "setup_cancel":
        async def blocked_dc(_dc):
            t.reached.set()
            await asyncio.Event().wait()
        client._get_dc.side_effect = blocked_dc
    task = asyncio.create_task(parallel.download_document(
        client, media(size), str(tmp_path / "out"), size,
        progress_callback=lambda *_: asyncio.sleep(0), check_scope=lambda: None,
        refresh_document=refresh, slots=asyncio.Semaphore(capacity), connections=pool))
    try:
        await asyncio.wait_for(t.reached.wait(), 10)
        if outcome in {"cancel", "setup_cancel"}:
            task.cancel()
        else:
            t.gate.set()
        if outcome in {"cancel", "setup_cancel"}:
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "failure":
            with pytest.raises(ValueError, match="bad media"):
                await task
        else:
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert t.state.active == 0
    assert client._sender.closed == 0
    created = [s for s in t.made if s not in (client._sender,
               client._borrow_exported_sender.return_value)]
    assert all(s.closed >= 1 and not s.pending for s in created)
    assert client.session.dc_id == 2
    if outcome == "expired":
        refresh.assert_awaited_once()
    if outcome == "migrate":
        client._borrow_exported_sender.assert_awaited_once_with(3)
        client._return_exported_sender.assert_awaited_once()
    if outcome == "migrate_twice":
        assert client._borrow_exported_sender.await_count == 2
        assert client._return_exported_sender.await_count == 2
    if outcome == "cdn":
        fallback.assert_awaited_once()
    # A cancelled/failed lease must leave every queue slot available to a later
    # transfer; checking behavior avoids coupling to private queue bookkeeping.
    t.state.outcome = "success"
    t.gate.set()
    client._get_dc.side_effect = lambda dc_id: SimpleNamespace(
        id=dc_id, ip_address="127.0.0.1", port=443)
    await asyncio.wait_for(parallel.download_document(
        client, media(size), str(tmp_path / "again"), size,
        progress_callback=lambda *_: asyncio.sleep(0), check_scope=lambda: None,
        refresh_document=refresh, slots=asyncio.Semaphore(capacity), connections=pool), 10)
    assert (tmp_path / "again").stat().st_size == size


@pytest.mark.asyncio
async def test_repeated_cancellation_during_final_disconnect_does_not_strand_pool_capacity(
        tmp_path, transport, monkeypatch):
    t = transport
    client = t.client()
    size = 4 * PART
    t.state.size = size
    t.gate.set()
    pool = connections.DownloadConnectionPool(4)
    closing = asyncio.Event()
    release_close = asyncio.Event()
    first_close = True
    disconnect = t.sender_type.disconnect

    async def blocked_disconnect(sender):
        nonlocal first_close
        if first_close:
            first_close = False
            closing.set()
            await release_close.wait()
        await disconnect(sender)

    monkeypatch.setattr(t.sender_type, "disconnect", blocked_disconnect)

    async def run(name):
        return await parallel.download_document(
            client, media(size), str(tmp_path / name), size,
            progress_callback=lambda *_: asyncio.sleep(0), check_scope=lambda: None,
            refresh_document=lambda: pytest.fail("unexpected refresh"),
            slots=asyncio.Semaphore(4), connections=pool)

    task = asyncio.create_task(run("cancel-during-close"))
    try:
        await asyncio.wait_for(closing.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(sender.closed == 1 for sender, _ in t.requests)
    assert client._sender.closed == 0
    # This hangs if the cancelled finalizer drained any queue slot without
    # restoring it. A new complete transfer must work with the same pool.
    result = await asyncio.wait_for(run("after-cancelled-close"), 10)
    assert result["sender_mode"] == "independent_connections"
    assert (tmp_path / "after-cancelled-close").stat().st_size == size


@pytest.mark.asyncio
async def test_full_pool_replaces_senders_for_different_client_and_rotated_auth_key(transport):
    from contextlib import AsyncExitStack

    t = transport
    pool = connections.DownloadConnectionPool()
    for invalid in (0, pool.capacity + 1):
        with pytest.raises(ValueError):
            connections.DownloadConnectionPool(invalid)
    first, second = t.client(), t.client()
    epochs = []
    # Keep the pool alive between epochs so identity/key validation, rather than
    # ordinary last-transfer teardown, must disconnect the previous senders.
    async with pool.transfer(first):
        for client, rotate in ((first, False), (second, False), (second, True), (first, False)):
            if rotate:
                client._sender.auth_key = AuthKey(bytes([93]) * 256)
            async with pool.transfer(client), AsyncExitStack() as stack:
                facades = [await stack.enter_async_context(pool.connection(client, 2))
                           for _ in range(pool.capacity)]
                current = {facade._sender for facade in facades}
                assert len(current) == pool.capacity
                assert all(facade._main_client is client for facade in facades)
                assert client._sender not in current
                assert all(sender.auth_key.key == client._sender.auth_key.key for sender in current)
                assert all(sender.auth_key is not client._sender.auth_key for sender in current)
                for previous in epochs:
                    assert not current & previous
                    assert all(sender.closed == 1 for sender in previous)
                epochs.append(current)
    assert all(sender.closed == 1 and not sender.pending for epoch in epochs for sender in epoch)
    assert first._sender.closed == second._sender.closed == 0
    assert first.session.dc_id == second.session.dc_id == 2
