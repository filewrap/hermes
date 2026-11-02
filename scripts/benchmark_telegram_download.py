#!/usr/bin/env python3
"""Offline synthetic throughput/concurrency harness for Telegram document downloads.

This exercises Telethon 1.45's real ``TelegramClient._call`` and
``DownloadMethods.iter_download`` code while replacing every MTProto sender and
connection operation with an in-process deterministic simulator. It never
contacts Telegram and its synthetic rates are not predictions of live service
performance.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from telethon import TelegramClient, functions, types
from telethon.crypto.authkey import AuthKey
from telethon.sessions import MemorySession
from telethon.tl.functions.upload import GetFileRequest

from plugins.platforms.telegram import download_connections, parallel_download

PART_BYTES = 512 * 1024
_PATTERN_MULTIPLIER = 17
_PATTERN_ADDEND = 23
_PATTERN_MODULUS = 251
_BYTE_PATTERN = bytes((i * _PATTERN_MULTIPLIER + _PATTERN_ADDEND) % _PATTERN_MODULUS
                      for i in range(_PATTERN_MODULUS))
_CURRENT_SIMULATION: Simulation | None = None


def payload_bytes(offset: int, length: int) -> bytes:
    """Deterministic offset-derived content, so ranges and order are verifiable."""
    if length <= 0:
        return b""
    start = offset % _PATTERN_MODULUS
    first_length = min(length, _PATTERN_MODULUS - start)
    first = _BYTE_PATTERN[start:start + first_length]
    remaining = length - first_length
    return first + _BYTE_PATTERN * (remaining // _PATTERN_MODULUS) + _BYTE_PATTERN[:remaining % _PATTERN_MODULUS]


def payload_sha256(size: int) -> str:
    digest = hashlib.sha256()
    block = 1024 * 1024
    for offset in range(0, size, block):
        digest.update(payload_bytes(offset, min(block, size - offset)))
    return digest.hexdigest()


class Simulation:
    """Synthetic request RTT and byte-rate gates; contains no sockets or IO."""

    def __init__(self, *, rtt_seconds: float, per_connection_bytes_per_second: float | None,
                 global_bytes_per_second: float | None):
        self.rtt_seconds = rtt_seconds
        self.per_connection_bytes_per_second = per_connection_bytes_per_second
        self.global_bytes_per_second = global_bytes_per_second
        self.global_link_lock = asyncio.Lock()
        self.senders: list[FakeSender] = []
        self.file_sender_ids: set[int] = set()
        self.active_file_requests = 0
        self.peak_file_requests = 0
        self.total_file_requests = 0
        self.file_size = 0
        self._counter = 0

    def new_sender(self, auth_key: Any, **kwargs: Any) -> FakeSender:
        self._counter += 1
        sender = FakeSender(auth_key, self, sender_id=self._counter, **kwargs)
        self.senders.append(sender)
        return sender

    async def respond(self, sender: FakeSender, request: Any) -> Any:
        while isinstance(request, functions.InvokeWithoutUpdatesRequest):
            request = request.query
        if not isinstance(request, GetFileRequest):
            # Telethon's independent sender is initialized with InvokeWithLayer;
            # MemorySession accepts this empty entity collection.
            return []

        self.total_file_requests += 1
        self.file_sender_ids.add(sender.sender_id)
        self.active_file_requests += 1
        self.peak_file_requests = max(self.peak_file_requests, self.active_file_requests)
        try:
            if self.rtt_seconds:
                await asyncio.sleep(self.rtt_seconds)
            offset = int(request.offset)
            # The real iterator can request a full part at the EOF; Telegram
            # returns only the bytes remaining in the declared document.
            file_size = self.file_size or sender.file_size
            length = max(0, min(int(request.limit), file_size - offset))

            async def transmit(lock: asyncio.Lock | None, rate: float | None) -> None:
                if lock is None or rate is None or rate <= 0:
                    return
                async with lock:
                    await asyncio.sleep(length / rate)

            await transmit(sender.byte_lock, self.per_connection_bytes_per_second)
            await transmit(self.global_link_lock, self.global_bytes_per_second)
            return types.upload.File(type="unknown", mtime=0,
                                     bytes=payload_bytes(offset, length))
        finally:
            self.active_file_requests -= 1


class FakeSender:
    """In-memory MTProtoSender stand-in with task and lifecycle accounting."""

    def __init__(self, auth_key: Any, simulation: Simulation | None = None, *,
                 sender_id: int | None = None, **_kwargs: Any):
        selected = simulation or _CURRENT_SIMULATION
        if selected is None:
            raise RuntimeError("fake sender created outside benchmark simulation")
        self.simulation = selected
        if sender_id is None:
            selected._counter += 1
            sender_id = selected._counter
            selected.senders.append(self)
        self.sender_id = sender_id
        self.auth_key = auth_key
        self.connected = False
        self.byte_lock = asyncio.Lock()
        self.pending: set[asyncio.Task[Any]] = set()
        self.file_size = 0
        self.connection_state: Any = None
        self.disconnect_count = 0

    async def connect(self, connection: Any) -> bool:
        self.connection_state = connection
        self.connected = True
        return True

    def is_connected(self) -> bool:
        return self.connected

    def send(self, request: Any, ordered: bool = False) -> asyncio.Task[Any]:
        del ordered
        task = asyncio.create_task(self.simulation.respond(self, request))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        return task

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1
        pending = [task for task in self.pending if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class SharedPoolFallback:
    """Pool-shaped input that deliberately forces parallel_download's shared path."""

    capacity = 1

    @staticmethod
    def supports(_client: Any) -> bool:
        return False


SCENARIOS = (
    # Individual stream ceiling; enough aggregate-link headroom to expose sender count.
    ("connection_limited", 8 * 1024 * 1024, 64 * 1024 * 1024),
    # Aggregate bottleneck dominates; more connections cannot raise link throughput.
    ("link_limited", 64 * 1024 * 1024, 4 * 1024 * 1024),
    # No bandwidth ceiling: per-request RTT is the only intentional transport delay.
    ("latency_limited", None, None),
)


def _make_client(simulation: Simulation) -> TelegramClient:
    """Build a genuine, disconnected Telethon client with a synthetic authorized key."""
    global _CURRENT_SIMULATION
    _CURRENT_SIMULATION = simulation
    session = MemorySession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(i % 251 for i in range(256)))
    client = TelegramClient(session, 1, "benchmark-api-hash-not-a-credential")
    # The patched Telethon constructor has already created the fake main sender;
    # no client.connect() or other operation that can open a socket is called.
    client._sender.connected = True
    client._sender.file_size = 0

    async def local_dc(dc_id: int, cdn: bool = False) -> Any:
        del cdn
        return SimpleNamespace(id=dc_id, ip_address="127.0.0.1", port=443)

    client._get_dc = local_dc
    client._connection = lambda *_args, **_kwargs: object()
    return client


def _document(size: int) -> types.Document:
    return types.Document(id=987654321, access_hash=123456789, file_reference=b"offline-reference",
                          date=0, mime_type="application/octet-stream", size=size,
                          dc_id=2, attributes=[])


async def run_case(*, scenario: str, concurrency: int, independent: bool,
                   size: int, rtt_seconds: float, per_connection_rate: float | None,
                   global_rate: float | None, temp_root: Path) -> dict[str, Any]:
    simulation = Simulation(rtt_seconds=rtt_seconds,
                            per_connection_bytes_per_second=per_connection_rate,
                            global_bytes_per_second=global_rate)
    simulation.file_size = size
    expected_sha = payload_sha256(size)
    mode = (f"independent-{concurrency}" if independent else
            {1: "shared-single", 4: "shared-four"}.get(concurrency, f"shared-{concurrency}"))
    label = f"{scenario}/{mode}"
    client: TelegramClient | None = None
    started = time.perf_counter()
    global _CURRENT_SIMULATION
    _CURRENT_SIMULATION = simulation
    try:
        with ExitStack() as stack:
            # The pool and Telethon's constructor each hold a module-level alias;
            # patch those lookup sites so no real sender can be instantiated.
            stack.enter_context(patch("telethon.network.mtprotosender.MTProtoSender", FakeSender))
            stack.enter_context(patch("telethon.client.telegrambaseclient.MTProtoSender", FakeSender))
            stack.enter_context(patch.object(download_connections, "MTProtoSender", FakeSender))
            stack.enter_context(patch.object(parallel_download, "PARALLEL_PARTS", concurrency))
            client = _make_client(simulation)
            client._sender.file_size = size
            pool = download_connections.DownloadConnectionPool(capacity=concurrency) if independent else SharedPoolFallback()

            async def no_progress(_current: int, _total: int) -> None:
                return None

            with tempfile.TemporaryDirectory(prefix="telegram-download-bench-", dir=temp_root) as temp_dir:
                output_path = Path(temp_dir) / "download.bin"
                result = await parallel_download.download_document(
                    client, SimpleNamespace(media=SimpleNamespace(document=_document(size))),
                    str(output_path), size,
                    progress_callback=no_progress,
                    check_scope=lambda: None,
                    refresh_document=lambda: _unexpected_refresh(),
                    slots=asyncio.Semaphore(concurrency),
                    connections=pool,
                )
                elapsed = time.perf_counter() - started
                output_size = output_path.stat().st_size
                output_digest = hashlib.sha256()
                with output_path.open("rb") as completed_file:
                    for block in iter(lambda: completed_file.read(1024 * 1024), b""):
                        output_digest.update(block)
                digest = output_digest.hexdigest()
                verified = output_size == size and digest == expected_sha
                if not verified:
                    raise RuntimeError(f"{label}: output verification failed ({output_size} bytes, sha256={digest})")

        file_senders = simulation.file_sender_ids
        expected_senders = min(concurrency, math.ceil(size / PART_BYTES)) if independent else 1
        if len(file_senders) != expected_senders or simulation.peak_file_requests > concurrency:
            raise RuntimeError(f"{label}: sender isolation or request-concurrency bound violated")
        if independent and any(sender.connected or sender.pending for sender in simulation.senders
                               if sender is not client._sender):
            raise RuntimeError(f"{label}: dedicated sender survived the transfer")
        return {
            "scenario": scenario,
            "mode": mode,
            "request_concurrency": concurrency,
            "document_size_bytes": size,
            "elapsed_seconds": round(elapsed, 6),
            "throughput_mib_per_second": round(size / (1024 * 1024) / elapsed, 6),
            "peak_active_requests": simulation.peak_file_requests,
            "distinct_senders": len(file_senders),
            "file_request_count": simulation.total_file_requests,
            "output_size_bytes": output_size,
            "output_sha256": digest,
            "verified": verified,
            "synthetic_rtt_ms": round(rtt_seconds * 1000, 3),
            "synthetic_per_connection_mib_per_second": _mib_rate(per_connection_rate),
            "synthetic_global_link_mib_per_second": _mib_rate(global_rate),
            "sender_mode": result["sender_mode"],
            "benchmark_note": "synthetic offline model; not live Telegram throughput",
        }
    finally:
        if client is not None:
            for sender in simulation.senders:
                await sender.disconnect()
            # TelegramClient is intentionally never connected or disconnected;
            # only its in-memory fake senders are closed above.
        _CURRENT_SIMULATION = None


def _unexpected_refresh() -> Any:
    raise AssertionError("synthetic benchmark should not refresh the document")


def _mib_rate(value: float | None) -> float | None:
    return None if value is None else round(value / (1024 * 1024), 3)


async def benchmark(*, size: int, rtt_seconds: float, output_path: Path | None) -> dict[str, Any]:
    cache_dir = REPO_ROOT / ".pytest_cache"
    cache_dir.mkdir(exist_ok=True)
    results: list[dict[str, Any]] = []
    for scenario, per_connection_rate, global_rate in SCENARIOS:
        # One transfer per case; the real rolling iterator issues one, two, or
        # four concurrent part requests. Shared single/four explicitly compare
        # one sender at concurrency 1 vs concurrency 4.
        for concurrency in (1, 2, 4):
            for independent in (False, True):
                results.append(await run_case(
                    scenario=scenario, concurrency=concurrency, independent=independent,
                    size=size, rtt_seconds=rtt_seconds,
                    per_connection_rate=per_connection_rate,
                    global_rate=global_rate, temp_root=cache_dir,
                ))
    report = {
        "benchmark": "telegram_download_synthetic_offline",
        "telethon_version": __import__("telethon").__version__,
        "network_access": "none; all MTProto senders, DC lookups, and connections are simulated",
        "default_document_size_bytes": size,
        "cases": results,
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=float, default=8.0,
                        help="document size in MiB before a 37-byte short final part (default: 8)")
    parser.add_argument("--rtt-ms", type=float, default=30.0,
                        help="synthetic per-part request RTT in milliseconds (default: 30)")
    parser.add_argument("--output", type=Path, default=None,
                        help="optional JSON report path; otherwise print JSON to stdout")
    args = parser.parse_args(argv)
    if not math.isfinite(args.size_mib) or args.size_mib <= 0:
        parser.error("--size-mib must be a finite positive number")
    if not math.isfinite(args.rtt_ms) or args.rtt_ms < 0:
        parser.error("--rtt-ms must be a finite non-negative number")
    args.size_bytes = int(args.size_mib * 1024 * 1024) + 37
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = asyncio.run(benchmark(size=args.size_bytes, rtt_seconds=args.rtt_ms / 1000,
                                   output_path=args.output))
    if args.output is None:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
