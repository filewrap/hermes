"""Bounded, local-only throughput diagnostics for yielded MTProto payloads."""
from __future__ import annotations

import time
from typing import Any


INTERVAL_SECONDS = 10
MAX_INTERVALS = 360  # One hour at full resolution; later arrivals share one tail.
DECIMAL_MB = 1_000_000


class ArrivalThroughput:
    """Count iterator payload arrivals, not writes or cumulative UI progress.

    This is application-observed payload throughput, not socket/wire throughput:
    Telethon's internal retries are invisible. Every yielded payload is counted,
    including a payload returned again after a caller-level retry. No payload,
    part offset, identity or absolute timestamp is retained. Storage and final
    log size are bounded even for arbitrarily long/slow downloads.
    """

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self._counts = [0] * MAX_INTERVALS
        self._overflow_bytes = 0
        self._received_bytes = 0
        self._first_arrival: float | None = None
        self._last_arrival: float | None = None

    def record(self, received_bytes: int) -> None:
        if received_bytes <= 0:
            return
        elapsed = time.monotonic() - self.started_at
        index = int(elapsed // INTERVAL_SECONDS)
        if index < MAX_INTERVALS:
            self._counts[index] += received_bytes
        else:
            self._overflow_bytes += received_bytes
        self._received_bytes += received_bytes
        if self._first_arrival is None:
            self._first_arrival = elapsed
        self._last_arrival = elapsed

    def summary(self) -> dict[str, Any]:
        elapsed = max(0.0, time.monotonic() - self.started_at)

        def interval(start: int, end: float, count: int) -> dict[str, Any]:
            duration = max(0.0, end - start)
            return {"start_seconds": start, "duration_seconds": duration,
                    "received_mb": count / DECIMAL_MB,
                    "mb_per_second": count / DECIMAL_MB / duration if duration else None}

        # Half-open [start, end) buckets. An arrival exactly on a boundary
        # belongs to the next bucket, even if completion makes it zero-duration;
        # its rate is then unknown (None), never divided by a fabricated epsilon.
        last_index = int(elapsed // INTERVAL_SECONDS)
        buckets = [interval(i * INTERVAL_SECONDS,
                            min(elapsed, (i + 1) * INTERVAL_SECONDS), self._counts[i])
                   for i in range(min(last_index + 1, MAX_INTERVALS))]
        tail_start = MAX_INTERVALS * INTERVAL_SECONDS
        return {"measurement": "mtproto_iterator_payload_arrivals",
                "time_origin": "download_helper_entry_before_connection_setup",
                "units": "decimal MB and MB/s (1 MB = 1000000 bytes)",
                "interval_seconds": INTERVAL_SECONDS,
                "max_intervals": MAX_INTERVALS,
                "elapsed_seconds": elapsed,
                "first_arrival_seconds": self._first_arrival,
                "last_arrival_seconds": self._last_arrival,
                "received_bytes": self._received_bytes,
                "received_mb": self._received_bytes / DECIMAL_MB,
                "intervals": buckets,
                "overflow_interval": (interval(tail_start, elapsed, self._overflow_bytes)
                                      if last_index >= MAX_INTERVALS else None)}
