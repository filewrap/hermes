"""Generalised launch idempotency for agent-internal and cron launches.

External-agent shape (``src/gateway/server-methods/session-create-idempotency.ts``):
caller-supplied ``idempotencyKey`` + sha256 request-identity hash + in-flight
join + TTL replay, bounded by ``DEDUPE_MAX``. Hermes already had the durable
half for ``POST /v1/runs`` (``RunIdempotencyStore``); this module is the
reusable other half: any launcher reserves ``(scope, key)`` once, joins an
in-flight duplicate in this process, replays a terminal outcome inside
retention, and gets a conflict when the same key arrives with a different
request identity. A failed run releases its reservation, so a retry can run
it again — the same rule the external agent applies (failed entries are released, only
successful ones become replayable).

Cross-process duplicates cannot join a live run they cannot see, so a reused
non-terminal reservation whose owner is provably dead is adopted (the caller
re-reserves after forgetting the stale row), while one whose owner is alive
is reported ``running`` and the caller must not start a second run.

The store is constructed per ``LaunchIdempotency``, never cached at module
level: under multiplexing the owning home is bound per activity
(``gateway/AGENTS.md``), and a module-global store would freeze the launch
profile's home.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from gateway.platforms.api_server_run_idempotency import (
    TERMINAL_STATUSES,
    RunIdempotencyStore,
)

logger = logging.getLogger("gateway.platforms.launch_idempotency")

# The external agent bounds process-wide dedupe state at DEDUPE_MAX entries per owner
# (src/gateway/server-constants.ts:10); the same bound caps the in-process
# in-flight registries here (one map per launcher, not per owner).
MAX_INFLIGHT = 1000

LaunchOutcomeKind = Literal["created", "replayed", "joined", "running"]


class LaunchIdempotencyConflict(ValueError):
    """The same ``(scope, key)`` was reused with a different request identity."""


class LaunchIdempotencyBusy(RuntimeError):
    """The in-flight registry is full; retry the launch later."""


@dataclass
class LaunchOutcome:
    """What ``launch_once`` / ``alaunch_once`` did. ``result`` is the run's
    return value when this call ran it or joined it in-process; ``None`` on a
    durable replay or when another process owns the live run. ``record`` is
    the stored reservation (``None`` when this call created it and ran)."""

    outcome: LaunchOutcomeKind
    record: dict[str, Any] | None = None
    result: Any = None


def request_fingerprint(payload: Any) -> str:
    """sha256 of the canonical JSON of *payload* (the external agent's ``stableStringify``
    + ``sha256Hex`` shape). Key order does not change the identity; anything
    that is not JSON-serializable raises, so callers must pass plain data."""
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _owner_pid_started() -> tuple[int, int]:
    """``(pid, process start time)`` stamped on a reservation we own."""
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_time

        return pid, int(get_process_start_time(pid) or 0)
    except Exception:
        logger.debug("launch_idempotency: process start time unreadable", exc_info=True)
        return pid, 0


def _owner_alive(owner_pid: int, owner_started: int) -> bool:
    """True when the recorded owner pid still exists and is the same process
    incarnation (mirrors ``_owner_alive`` in
    ``gateway/platforms/api_server_runs.py:443``)."""
    try:
        from gateway.status import (
            _pid_exists,
            get_process_start_time,
            start_time_fingerprints_match,
        )

        return (
            owner_pid > 0
            and bool(_pid_exists(owner_pid))
            and (
                not owner_started
                or start_time_fingerprints_match(
                    owner_started, get_process_start_time(owner_pid) or 0
                )
            )
        )
    except Exception:
        logger.debug("launch_idempotency: owner liveness unreadable", exc_info=True)
        return False


@dataclass
class _Flight:
    """One in-flight launch: waiters block on ``event`` and read ``box``."""

    event: Any  # threading.Event or asyncio.Event
    box: dict[str, Any]  # {"result": ...} or {"error": BaseException}


class LaunchIdempotency:
    """Durable ``(scope, key)`` launch reservations over ``RunIdempotencyStore``."""

    def __init__(
        self,
        store: RunIdempotencyStore | None = None,
        *,
        max_inflight: int = MAX_INFLIGHT,
    ):
        self._store = store if store is not None else RunIdempotencyStore()
        self._max_inflight = max(1, int(max_inflight))
        self._lock = threading.Lock()
        self._inflight_sync: dict[tuple[str, str, str], _Flight] = {}
        self._inflight_async: dict[tuple[str, str, str], _Flight] = {}

    # -- durable protocol -------------------------------------------------

    def _reserve_or_adopt(
        self,
        scope: str,
        key: str,
        fingerprint: str,
        run_id: str,
        status: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        """Reserve once; adopt a stale non-terminal reservation whose owner is
        provably dead (the cron dead-owner reclaim, applied to launches)."""
        owner_pid, owner_started = _owner_pid_started()
        outcome, record = self._store.reserve(
            scope,
            key,
            fingerprint,
            run_id,
            status,
            owner_pid=owner_pid,
            owner_started=owner_started,
        )
        if outcome != "reused" or record is None:
            return outcome, record
        status_name = (record.get("status") or {}).get("status")
        if status_name in TERMINAL_STATUSES:
            return outcome, record
        if _owner_alive(
            int(record.get("owner_pid") or 0), int(record.get("owner_started") or 0)
        ):
            return outcome, record
        # Stale owner: forget and re-reserve atomically enough — a second
        # adopter racing us gets "reused" against our fresh live row.
        self._store.forget(scope, key)
        return self._store.reserve(
            scope,
            key,
            fingerprint,
            run_id,
            status,
            owner_pid=owner_pid,
            owner_started=owner_started,
        )

    # -- synchronous launches (cron threads, gateway executors) -----------

    def launch_once(
        self,
        *,
        scope: str,
        key: str,
        fingerprint: str,
        run_id: str,
        run: Callable[[], Any],
        status_extra: dict[str, Any] | Callable[[Any], dict[str, Any]] | None = None,
    ) -> LaunchOutcome:
        """Run ``run()`` exactly once for ``(scope, key, fingerprint)``.

        ``created``: this call ran it. ``replayed``: a terminal outcome inside
        retention was returned without running. ``joined``: another thread in
        this process ran it while this call waited. ``running``: another
        process owns a live run; this call ran nothing. A ``run()`` exception
        releases the reservation and propagates (to the owner and to joiners).
        ``status_extra`` is merged into the terminal status — a dict, or a
        callable of the run's return value.
        """
        outcome, record = self._reserve_or_adopt(
            scope, key, fingerprint, run_id, {"status": "running"}
        )
        if outcome == "conflict":
            raise LaunchIdempotencyConflict(
                f"launch idempotency key {scope}:{key} was reused with different parameters"
            )
        if outcome == "reused" and record is not None:
            if (record.get("status") or {}).get("status") in TERMINAL_STATUSES:
                return LaunchOutcome("replayed", record=record)
            joined = self._join_sync(scope, key, fingerprint)
            if joined is not None:
                return joined
            return LaunchOutcome("running", record=record)
        flight_key = (scope, key, fingerprint)
        with self._lock:
            if len(self._inflight_sync) >= self._max_inflight:
                self._store.forget(scope, key)
                raise LaunchIdempotencyBusy(
                    "launch idempotency capacity is full; retry later"
                )
            flight = _Flight(threading.Event(), {})
            self._inflight_sync[flight_key] = flight
        try:
            result = run()
            flight.box["result"] = result
        except BaseException as exc:
            flight.box["error"] = exc
            self._store.forget(scope, key)
            raise
        finally:
            # The box is filled before the event fires: a woken joiner must
            # never read an empty box.
            with self._lock:
                self._inflight_sync.pop(flight_key, None)
            flight.event.set()
        terminal = {"status": "completed"}
        if status_extra:
            terminal.update(
                status_extra(result) if callable(status_extra) else status_extra
            )
        self._store.update_status(run_id, terminal)
        return LaunchOutcome("created", result=result)

    def _join_sync(
        self, scope: str, key: str, fingerprint: str
    ) -> LaunchOutcome | None:
        """Wait for an in-flight launch in this process; ``None`` when none."""
        with self._lock:
            flight = self._inflight_sync.get((scope, key, fingerprint))
        if flight is None:
            return None
        flight.event.wait()
        if "error" in flight.box:
            raise flight.box["error"]
        return LaunchOutcome("joined", result=flight.box.get("result"))

    # -- asynchronous launches (gateway wake turns) ------------------------

    async def alaunch_once(
        self,
        *,
        scope: str,
        key: str,
        fingerprint: str,
        run_id: str,
        run: Callable[[], Any],
        status_extra: dict[str, Any] | Callable[[Any], dict[str, Any]] | None = None,
    ) -> LaunchOutcome:
        """Async ``launch_once``: ``run`` is an async callable, joiners await
        an ``asyncio.Event`` instead of blocking the loop."""
        outcome, record = await asyncio.to_thread(
            self._reserve_or_adopt,
            scope,
            key,
            fingerprint,
            run_id,
            {"status": "running"},
        )
        if outcome == "conflict":
            raise LaunchIdempotencyConflict(
                f"launch idempotency key {scope}:{key} was reused with different parameters"
            )
        if outcome == "reused" and record is not None:
            if (record.get("status") or {}).get("status") in TERMINAL_STATUSES:
                return LaunchOutcome("replayed", record=record)
            joined = await self._join_async(scope, key, fingerprint)
            if joined is not None:
                return joined
            return LaunchOutcome("running", record=record)
        flight_key = (scope, key, fingerprint)
        with self._lock:
            if len(self._inflight_async) >= self._max_inflight:
                await asyncio.to_thread(self._store.forget, scope, key)
                raise LaunchIdempotencyBusy(
                    "launch idempotency capacity is full; retry later"
                )
            flight = _Flight(asyncio.Event(), {})
            self._inflight_async[flight_key] = flight
        try:
            result = await run()
            flight.box["result"] = result
        except BaseException as exc:
            flight.box["error"] = exc
            await asyncio.to_thread(self._store.forget, scope, key)
            raise
        finally:
            # The box is filled before the event fires: a woken joiner must
            # never read an empty box.
            with self._lock:
                self._inflight_async.pop(flight_key, None)
            flight.event.set()
        terminal = {"status": "completed"}
        if status_extra:
            terminal.update(
                status_extra(result) if callable(status_extra) else status_extra
            )
        await asyncio.to_thread(self._store.update_status, run_id, terminal)
        return LaunchOutcome("created", result=result)

    async def _join_async(
        self, scope: str, key: str, fingerprint: str
    ) -> LaunchOutcome | None:
        with self._lock:
            flight = self._inflight_async.get((scope, key, fingerprint))
        if flight is None:
            return None
        await flight.event.wait()
        if "error" in flight.box:
            raise flight.box["error"]
        return LaunchOutcome("joined", result=flight.box.get("result"))

    # -- scope helpers ------------------------------------------------------

    @staticmethod
    def wake_scope(profile: str) -> str:
        """Idempotency scope for gateway wake turns owned by *profile*."""
        return f"wake:{profile or 'default'}"

    @staticmethod
    def headless_scope() -> str:
        """Idempotency scope for generic headless runs (``cron/headless_run``)."""
        return "headless"
