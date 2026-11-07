"""Arrival telemetry is independent of write order, cleanup and UI delays."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from plugins.platforms.telegram import download_throughput as throughput
from plugins.platforms.telegram import parallel_download as parallel


@pytest.mark.parametrize("arrivals,finish,amounts,rates", [
    ([(3, 1_000_000)], 5, [1], [0.2]),
    ([(9.999, 1_000_000), (10, 2_000_000), (20, 500_000)], 25,
     [1, 2, 0.5], [0.1, 0.2, 0.1]),
    ([(10, 1_000_000)], 10, [0, 1], [0, None]),
    ([(25, 1_000_000)], 30, [0, 0, 1, 0], [0, 0, 0.1, None]),
    ([], 0, [0], [None]),
])
def test_half_open_buckets_decimal_rates_and_short_transfers(monkeypatch, arrivals, finish, amounts, rates):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(throughput, "time", SimpleNamespace(monotonic=lambda: clock.now))
    meter = throughput.ArrivalThroughput()
    for elapsed, count in arrivals:
        clock.now = 100 + elapsed
        meter.record(count)
    clock.now = 100 + finish
    summary = meter.summary()
    assert [b["received_mb"] for b in summary["intervals"]] == amounts
    assert [b["mb_per_second"] for b in summary["intervals"]] == rates
    assert summary["received_bytes"] == sum(count for _, count in arrivals)
    assert summary["received_mb"] == sum(amounts)
    assert summary["first_arrival_seconds"] == (pytest.approx(arrivals[0][0]) if arrivals else None)
    assert summary["last_arrival_seconds"] == (pytest.approx(arrivals[-1][0]) if arrivals else None)
    assert summary["overflow_interval"] is None


def test_arbitrarily_long_transfer_has_fixed_storage_and_one_overflow_interval(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(throughput, "time", SimpleNamespace(monotonic=lambda: clock.now))
    meter = throughput.ArrivalThroughput()
    meter.record(1_000_000)
    cap = throughput.MAX_INTERVALS * throughput.INTERVAL_SECONDS
    for i in range(10_000):
        clock.now = cap + i * 10_000
        meter.record(1_000_000)
    summary = meter.summary()
    assert len(meter._counts) == throughput.MAX_INTERVALS
    assert len(summary["intervals"]) == throughput.MAX_INTERVALS
    assert summary["received_bytes"] == 10_001_000_000
    assert sum(b["received_mb"] for b in summary["intervals"]) == 1
    tail = summary["overflow_interval"]
    assert tail["received_mb"] == 10_000
    assert tail["start_seconds"] == cap
    assert tail["mb_per_second"] == pytest.approx(10_000 / (clock.now - cap))
    # The accumulator retains only scalar counters/timestamps, never per-part
    # samples or payloads; fixed storage does not scale with arrival count/time.
    assert all(isinstance(v, (int, float, type(None))) for v in vars(meter).values()
               if v is not meter._counts)


@pytest.mark.asyncio
async def test_counts_yields_before_close_retry_and_blocked_progress_not_offset_order(tmp_path, monkeypatch):
    from telethon import types

    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(throughput, "time", SimpleNamespace(monotonic=lambda: clock.now))
    offsets = [parallel.PART_SIZE, 0]
    started = asyncio.Event()
    release = {offset: asyncio.Event() for offset in offsets}
    yielded = {offset: asyncio.Event() for offset in offsets}
    close_entered = asyncio.Event()
    finish_close = asyncio.Event()
    progress_entered = asyncio.Event()
    finish_progress = asyncio.Event()
    attempts = 0

    class Stream:
        def __init__(self, offset):
            self.offset = offset

        async def __anext__(self):
            nonlocal attempts
            started.set()
            if self.offset == 0:
                attempts += 1
                if attempts == 1:
                    # Failed requests have no yielded bytes to count.
                    raise ConnectionError("transient")
            await release[self.offset].wait()
            yielded[self.offset].set()
            return b"x" * parallel.PART_SIZE

        async def close(self):
            if self.offset == parallel.PART_SIZE:
                close_entered.set()
                await finish_close.wait()

    class Client:
        def iter_download(self, _document, *, offset, **_kwargs):
            return Stream(offset)

    class Pool:
        capacity = parallel.PARALLEL_PARTS

        def supports(self, client):
            return True

        @asynccontextmanager
        async def transfer(self, client):
            clock.now = 2  # Setup must remain inside the t=0 arrival timeline.
            yield

        @asynccontextmanager
        async def connection(self, client, dc_id):
            yield client

    async def progress(*_args):
        progress_entered.set()
        await finish_progress.wait()

    async def no_retry_delay(_delay):
        pass

    # Replace only this module's asyncio namespace, not the event-loop clock.
    monkeypatch.setattr(parallel, "asyncio", SimpleNamespace(
        **{name: getattr(asyncio, name) for name in
           ("Lock", "create_task", "wait", "FIRST_COMPLETED", "gather", "to_thread")},
        sleep=no_retry_delay))
    size = 2 * parallel.PART_SIZE
    doc = types.Document(id=1, access_hash=2, file_reference=b"r", date=1,
                         mime_type="application/octet-stream", size=size, dc_id=2, attributes=[])
    msg = SimpleNamespace(media=SimpleNamespace(document=doc))
    task = asyncio.create_task(parallel.download_document(
        Client(), msg, str(tmp_path / "out"), size, progress_callback=progress,
        check_scope=lambda: None, refresh_document=lambda: pytest.fail("unexpected refresh"),
        slots=asyncio.Semaphore(parallel.PARALLEL_PARTS), connections=Pool()))
    try:
        await asyncio.wait_for(started.wait(), 5)
        clock.now = 5
        release[parallel.PART_SIZE].set()
        await asyncio.wait_for(close_entered.wait(), 5)
        # First payload arrived at t=5, despite iterator cleanup blocking to 15.
        clock.now = 15
        finish_close.set()
        await asyncio.wait_for(progress_entered.wait(), 5)
        clock.now = 25
        release[0].set()
        await asyncio.wait_for(yielded[0].wait(), 5)
        clock.now = 35
    finally:
        finish_close.set()
        finish_progress.set()
        for event in release.values():
            event.set()
        stats = await task
    summary = stats["arrival_throughput"]
    assert stats["retry_count"] == 1
    assert summary["coverage"] == "parallel_download"
    assert summary["received_bytes"] == size
    assert [b["received_mb"] for b in summary["intervals"]] == [
        parallel.PART_SIZE / 1_000_000, 0, parallel.PART_SIZE / 1_000_000, 0]
    assert summary["first_arrival_seconds"] == 5
    assert summary["last_arrival_seconds"] == 25
    assert (tmp_path / "out").read_bytes() == b"x" * size
