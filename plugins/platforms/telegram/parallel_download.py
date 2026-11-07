"""Bounded, completion-driven MTProto downloads using Telethon's native iterator.

The pinned connection adapter provides independent download senders; all range,
migration, file-reference and CDN semantics remain owned by Telethon/the caller.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from telethon import errors, types
from telethon.client.downloads import _CdnRedirect

from .download_connections import MAX_DOWNLOAD_CONNECTIONS, DownloadConnectionPool
from .download_throughput import ArrivalThroughput

PART_SIZE = 512 * 1024
# Five-part trial: at most 2.5 MiB of retained part payloads per file window.
# client_operations shares this request/sender budget across simultaneous files.
PARALLEL_PARTS = MAX_DOWNLOAD_CONNECTIONS
MIN_PARALLEL_SIZE = 2 * PART_SIZE
PART_ATTEMPTS = 4


class IncompletePartError(Exception):
    """The server did not return the expected range of the declared document."""


def supports_parallel(message: Any, size: int | None) -> bool:
    document = getattr(getattr(message, "media", None), "document", None)
    return isinstance(document, types.Document) and size is not None and size >= MIN_PARALLEL_SIZE


async def download_document(client: Any, message: Any, path: str, size: int, *,
                            progress_callback: Any, check_scope: Any,
                            refresh_document: Any, slots: asyncio.Semaphore,
                            retry_callback: Any = None,
                            connections: DownloadConnectionPool | None = None) -> dict[str, Any]:
    """Own the connection lifetime until all part tasks have closed their iterators."""
    arrivals = ArrivalThroughput()
    pool = connections if connections is not None else DownloadConnectionPool(PARALLEL_PARTS)
    kwargs = dict(progress_callback=progress_callback, check_scope=check_scope,
                  refresh_document=refresh_document, slots=slots, retry_callback=retry_callback,
                  arrivals=arrivals)
    if pool.supports(client):
        async with pool.transfer(client):
            stats = await _download_document(client, message, path, size, connections=pool, **kwargs)
    else:
        # Unreviewed Telethon versions retain the public iterator, not private APIs.
        stats = await _download_document(client, message, path, size, connections=None, **kwargs)
    if "arrival_throughput" not in stats:
        stats["arrival_throughput"] = arrivals.summary()
    # Native CDN writes do not expose iterator arrivals here. Never label a
    # partial parallel-attempt measurement as throughput of the fallback file.
    stats["arrival_throughput"]["coverage"] = (
        "parallel_attempt_only_excludes_native_cdn_fallback"
        if stats["strategy"] == "native_cdn_fallback" else "parallel_download")
    return stats


async def _download_document(client: Any, message: Any, path: str, size: int, *,
                             progress_callback: Any, check_scope: Any,
                             refresh_document: Any, slots: asyncio.Semaphore,
                             retry_callback: Any, connections: DownloadConnectionPool | None,
                             arrivals: ArrivalThroughput) -> dict[str, Any]:
    """Keep at most five 512-KiB parts in memory and write at verified offsets.

    The per-operations request semaphore AND connection pool bound overlapping
    downloads. Telethon handles migration and small flood waits; retries refresh
    only the same scoped document. No part owns the main client's connection.
    """
    document = message.media.document
    refresh_lock = asyncio.Lock()
    active_requests = 0
    stats = {"strategy": "rolling_mtproto_window", "part_size_bytes": PART_SIZE,
             "write_order": "completion_offset",
             "window_parts": PARALLEL_PARTS,
             "sender_mode": "independent_connections" if connections else "shared_per_dc",
             "connection_limit": connections.capacity if connections else 1,
             "peak_active_part_requests": 0,
             "connection_acquire_seconds_sum": 0.0,
             "retry_count": 0, "requested_retry_wait_seconds": 0.0,
             "part_request_seconds_sum": 0.0, "semaphore_wait_seconds_sum": 0.0,
             "disk_write_seconds": 0.0, "progress_callback_seconds": 0.0}

    async def read_part(download_client: Any, current_document: Any, offset: int) -> bytes:
        nonlocal active_requests
        # limit=1 avoids prefetch and keeps every request aligned.
        stream = download_client.iter_download(current_document, offset=offset, limit=1,
                                               request_size=PART_SIZE, chunk_size=PART_SIZE,
                                               file_size=size)
        requested_at = time.monotonic()
        active_requests += 1
        stats["peak_active_part_requests"] = max(stats["peak_active_part_requests"], active_requests)
        try:
            data = await anext(stream, b"")
            # No await between arrival and accounting: close(), scope checks,
            # reordered writes and UI backpressure must not shift its bucket.
            arrivals.record(len(data))
            data = bytes(data)
            return data
        finally:
            try:
                await stream.close()
            finally:
                active_requests -= 1
                stats["part_request_seconds_sum"] += time.monotonic() - requested_at

    async def part(offset: int) -> bytes:
        nonlocal document
        expected = min(PART_SIZE, size - offset)
        for attempt in range(PART_ATTEMPTS):
            check_scope()
            current_document = document
            try:
                queued_at = time.monotonic()
                async with slots:
                    stats["semaphore_wait_seconds_sum"] += time.monotonic() - queued_at
                    check_scope()
                    if connections is None:
                        data = await read_part(client, current_document, offset)
                    else:
                        queued_at = time.monotonic()
                        async with connections.connection(client, current_document.dc_id) as download_client:
                            stats["connection_acquire_seconds_sum"] += time.monotonic() - queued_at
                            check_scope()
                            data = await read_part(download_client, current_document, offset)
                check_scope()
                if len(data) != expected:
                    raise IncompletePartError("Telegram returned an incomplete document part")
                return data
            except (errors.FileReferenceExpiredError, errors.FilerefUpgradeNeededError):
                if attempt == PART_ATTEMPTS - 1:
                    raise
                stats["retry_count"] += 1
                async with refresh_lock:
                    if document is current_document:
                        check_scope()
                        document = await refresh_document()
                        check_scope()
            except (TimeoutError, ConnectionError, errors.TimedOutError,
                    errors.ServerError, errors.FloodWaitError) as exc:
                if attempt == PART_ATTEMPTS - 1:
                    raise
                delay = max(0.5 * 2**attempt, float(getattr(exc, "seconds", 0)))
                stats["retry_count"] += 1
                stats["requested_retry_wait_seconds"] += delay
                if retry_callback is not None:
                    retry_callback(delay)
                await asyncio.sleep(delay)
        raise AssertionError("part retry loop exhausted")

    tasks: dict[asyncio.Task, int] = {}
    offsets = iter(range(0, size, PART_SIZE))
    written_bytes = 0

    def fill_window() -> None:
        while len(tasks) < PARALLEL_PARTS:
            offset = next(offsets, None)
            if offset is None:
                return
            check_scope()
            tasks[asyncio.create_task(part(offset))] = offset

    try:
        with await asyncio.to_thread(open, path, "wb") as output:
            fill_window()
            while tasks:
                done, waiting = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                del waiting
                while done:
                    pending = done.pop()
                    offset = tasks[pending]
                    data = pending.result()
                    check_scope()
                    write_started = time.monotonic()
                    output.seek(offset)
                    output.write(data)
                    stats["disk_write_seconds"] += time.monotonic() - write_started
                    written_bytes += len(data)
                    del tasks[pending]
                    # Drop the task's result before refilling: completed payloads
                    # still count toward the five-part memory bound until written.
                    del data, pending
                    fill_window()
                    callback_started = time.monotonic()
                    try:
                        # File position/size can include holes while earlier parts
                        # are pending; only committed part bytes represent progress.
                        await progress_callback(written_bytes, size)
                    finally:
                        stats["progress_callback_seconds"] += time.monotonic() - callback_started
    except _CdnRedirect:
        # Native writer owns CDN decryption/hash handling. Join siblings first.
        for pending in tasks:
            pending.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()
        check_scope()
        stats["strategy"] = "native_cdn_fallback"
        stats["sender_mode"] = "native_telethon"
        stats["arrival_throughput"] = arrivals.summary()
        await client.download_media(message, file=path, progress_callback=progress_callback)
    finally:
        for pending in tasks:
            pending.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return stats
