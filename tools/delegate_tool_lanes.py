"""Live per-(provider, model) lanes for delegated workers.

The quota is per model, so the concurrency cap must be per model too: a
single global count cannot see ten workers sharing one free-model quota.
The dispatcher acquires a lane slot when a child is submitted and releases
it when the child's run ends (however it ends); a spawn that would exceed
the lane's cap is refused with a reason, never silently queued.

Process-wide by design: two delegate calls at once share one quota, so
their lanes must be the same lanes. Tests reset via :func:`reset_lanes`.
"""

from __future__ import annotations

import threading
from typing import Optional

_LANES_LOCK = threading.Lock()
_LANES: dict[tuple[str, str], int] = {}


def lane_key(provider: Optional[str], model: Optional[str]) -> tuple[str, str]:
    """The lane a worker runs on: (provider, model), empty strings for inherit."""
    return (str(provider or ""), str(model or ""))


def try_acquire_lane(key: tuple[str, str], cap: int) -> bool:
    """Take one live slot on *key* when fewer than *cap* are live."""
    with _LANES_LOCK:
        live = _LANES.get(key, 0)
        if live >= max(1, int(cap)):
            return False
        _LANES[key] = live + 1
        return True


def release_lane(key: tuple[str, str]) -> None:
    """Return one live slot on *key*; never below zero."""
    with _LANES_LOCK:
        live = _LANES.get(key, 0)
        if live <= 1:
            _LANES.pop(key, None)
        else:
            _LANES[key] = live - 1


def reset_lanes() -> None:
    """Drop all lane state. Tests only — production lanes live for the process."""
    with _LANES_LOCK:
        _LANES.clear()
