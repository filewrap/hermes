"""Behaviour contracts for the generalised launch idempotency.

Covers the ported session-create shape: canonical fingerprint,
conflict on identity reuse, in-flight join (sync + async), TTL replay of a
terminal outcome, failure releasing the reservation, and stale-owner adoption.
"""

import asyncio
import os
import threading

import pytest

from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
from gateway.platforms.launch_idempotency import (
    LaunchIdempotency,
    LaunchIdempotencyBusy,
    LaunchIdempotencyConflict,
    request_fingerprint,
)
from gateway.status import get_process_start_time
from tests.waiting import wait_until


def _launcher(**kwargs):
    store = RunIdempotencyStore(db_path=":memory:")
    return LaunchIdempotency(store=store, **kwargs), store


def _spec(**overrides):
    spec = {"session_id": "sess-1", "text": "wake up", "category": "result"}
    spec.update(overrides)
    return spec


def test_fingerprint_is_canonical_and_identity_sensitive():
    assert request_fingerprint({"b": 1, "a": [2, 3]}) == request_fingerprint(
        {"a": [2, 3], "b": 1}
    )
    assert request_fingerprint(_spec()) != request_fingerprint(_spec(text="different"))
    assert request_fingerprint(_spec()) == request_fingerprint(_spec())


def test_replay_returns_terminal_without_rerunning():
    launcher, _store = _launcher()
    calls = []
    kwargs = dict(
        scope="wake:default",
        key="evt-1",
        fingerprint=request_fingerprint(_spec()),
        run_id="wake:default:evt-1",
    )
    first = launcher.launch_once(run=lambda: calls.append(1) or "done", **kwargs)
    assert first.outcome == "created" and first.result == "done"
    second = launcher.launch_once(run=lambda: calls.append(1) or "done", **kwargs)
    assert (
        second.outcome == "replayed"
        and second.record["status"]["status"] == "completed"
    )
    assert calls == [1]


def test_conflict_on_same_key_different_identity():
    launcher, _store = _launcher()
    base = dict(scope="wake:default", key="evt-1", run_id="wake:default:evt-1")
    launcher.launch_once(
        fingerprint=request_fingerprint(_spec()), run=lambda: None, **base
    )
    with pytest.raises(LaunchIdempotencyConflict):
        launcher.launch_once(
            fingerprint=request_fingerprint(_spec(text="other")),
            run=lambda: None,
            **base,
        )


def test_failure_releases_the_reservation():
    launcher, _store = _launcher()
    kwargs = dict(
        scope="wake:default",
        key="evt-1",
        fingerprint=request_fingerprint(_spec()),
        run_id="wake:default:evt-1",
    )

    def _flaky():
        if not _flaky.called:
            _flaky.called = True
            raise RuntimeError("boom")
        return "ok"

    _flaky.called = False

    with pytest.raises(RuntimeError, match="boom"):
        launcher.launch_once(run=_flaky, **kwargs)
    second = launcher.launch_once(run=_flaky, **kwargs)
    assert second.outcome == "created" and second.result == "ok"


def test_in_flight_join_runs_once_sync():
    launcher, _store = _launcher()
    started = threading.Event()
    release = threading.Event()
    joined = threading.Event()
    calls = []
    kwargs = dict(
        scope="headless",
        key="run-1",
        fingerprint=request_fingerprint(_spec()),
        run_id="headless:run-1",
    )

    def _slow():
        calls.append(1)
        started.set()
        assert release.wait(timeout=10)
        return "slow-done"

    results = {}

    def _call(name):
        results[name] = launcher.launch_once(run=_slow, **kwargs)

    orig_join = launcher._join_sync

    def _join_probe(*args, **kwargs):
        joined.set()
        return orig_join(*args, **kwargs)

    launcher._join_sync = _join_probe

    first = threading.Thread(target=_call, args=("first",))
    first.start()
    assert started.wait(timeout=10)
    second = threading.Thread(target=_call, args=("second",))
    second.start()
    assert wait_until(lambda: joined.is_set(), timeout=10)
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert calls == [1]
    assert results["first"].outcome == "created"
    assert results["second"].outcome == "joined"
    assert results["second"].result == "slow-done"


def test_stale_owner_is_adopted_not_blocked():
    launcher, store = _launcher()
    dead_pid = 2**30  # no such process on any host this runs on
    store.reserve(
        "wake:default",
        "evt-1",
        request_fingerprint(_spec()),
        "wake:default:evt-1",
        {"status": "running"},
        owner_pid=dead_pid,
        owner_started=0,
    )
    outcome = launcher.launch_once(
        scope="wake:default",
        key="evt-1",
        fingerprint=request_fingerprint(_spec()),
        run_id="wake:default:evt-1",
        run=lambda: "adopted",
    )
    assert outcome.outcome == "created" and outcome.result == "adopted"


def test_live_owner_elsewhere_reports_running():
    launcher, store = _launcher()
    store.reserve(
        "wake:default",
        "evt-1",
        request_fingerprint(_spec()),
        "wake:default:evt-1",
        {"status": "running"},
        owner_pid=os.getpid(),
        owner_started=int(get_process_start_time(os.getpid()) or 0),
    )
    outcome = launcher.launch_once(
        scope="wake:default",
        key="evt-1",
        fingerprint=request_fingerprint(_spec()),
        run_id="wake:default:evt-1",
        run=lambda: "must not run",
    )
    assert outcome.outcome == "running"


def test_in_flight_capacity_is_bounded():
    launcher, _store = _launcher(max_inflight=1)
    started = threading.Event()
    release = threading.Event()

    def _slow():
        started.set()
        assert release.wait(timeout=10)
        return "x"

    kwargs = dict(
        scope="headless",
        key="run-1",
        fingerprint=request_fingerprint(_spec()),
        run_id="headless:run-1",
    )
    worker = threading.Thread(target=lambda: launcher.launch_once(run=_slow, **kwargs))
    worker.start()
    assert started.wait(timeout=10)
    with pytest.raises(LaunchIdempotencyBusy):
        launcher.launch_once(
            scope="headless",
            key="run-2",
            fingerprint=request_fingerprint(_spec(text="t2")),
            run_id="headless:run-2",
            run=lambda: "y",
        )
    release.set()
    worker.join(timeout=10)


@pytest.mark.asyncio
async def test_async_replay_and_join():
    launcher, _store = _launcher()
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()
    joined = asyncio.Event()
    kwargs = dict(
        scope="wake:default",
        key="evt-9",
        fingerprint=request_fingerprint(_spec()),
        run_id="wake:default:evt-9",
    )

    async def _run():
        calls.append(1)
        started.set()
        await release.wait()
        return "async-done"

    orig_join = launcher._join_async

    async def _join_probe(*args, **kwargs):
        joined.set()
        return await orig_join(*args, **kwargs)

    launcher._join_async = _join_probe

    first = asyncio.create_task(launcher.alaunch_once(run=_run, **kwargs))
    await asyncio.wait_for(started.wait(), timeout=10)
    second = asyncio.create_task(launcher.alaunch_once(run=_run, **kwargs))
    await asyncio.wait_for(joined.wait(), timeout=10)
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert calls == [1]
    assert {first_result.outcome, second_result.outcome} == {"created", "joined"}
    joined_result = first_result if first_result.outcome == "joined" else second_result
    assert joined_result.result == "async-done"
    third = await launcher.alaunch_once(run=_run, **kwargs)
    assert third.outcome == "replayed" and calls == [1]
