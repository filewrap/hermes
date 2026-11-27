"""Behavior contracts for trusted isolated-worker job orchestration."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading

import pytest

from gateway.isolated_worker import Binding, BoundaryDenied, Peer
from gateway.isolated_worker.jobs import Destination, JobController


def binding(identity: str, room: str = "room", thread: str | None = None,
            generation: str = "grant-1") -> Binding:
    return Binding(identity, hashlib.sha256(generation.encode()).hexdigest(),
                   room, "forum" if thread else "group", thread)


def peer(identity: str, worker: str | None = None, *, room: str = "room",
         thread: str | None = None, generation: str = "grant-1") -> Peer:
    return Peer(worker or hashlib.md5(identity.encode()).hexdigest(),
                binding(identity, room, thread, generation),
                1000 + ord(identity[0]), "/hermes-workers/" + (worker or hashlib.md5(identity.encode()).hexdigest()))


class Dispatch:
    def __init__(self):
        self.sent = []
        self.started = None
        self.release = None

    async def send_text(self, identity, chat_id, chat_type, thread_id, text):
        self.sent.append((identity, chat_id, chat_type, thread_id, text))
        if self.started is not None:
            self.started.set()
            await self.release.wait()
        return "accepted"


def make_controller(*, peers, bindings, dispatch=None, **limits):
    return JobController(
        current_binding=lambda identity, chat, chat_type, thread: bindings.get(identity),
        peer_is_live=lambda candidate: peers.get(candidate.worker_id) == candidate,
        outbound=dispatch or Dispatch(), **limits,
    )


def test_inline_background_child_and_completion_receipt_are_peer_bound():
    a = peer("alice")
    b = peer("bob")
    peers, bindings = {a.worker_id: a, b.worker_id: b}, {"alice": a.binding, "bob": b.binding}
    controller = make_controller(peers=peers, bindings=bindings)

    root = controller.issue_root(a, {"prompt": "opaque input"}, kind="background")
    lease = controller.claim(a, root)
    assert lease.kind == "background"
    assert lease.payload == {"prompt": "opaque input"}
    child = controller.create_child(a, lease, {"task": "child"})
    child_lease = controller.claim(a, child)
    receipt_id = controller.complete(a, child_lease, {"answer": "done"})
    assert controller.consume_receipt(a, receipt_id) == {"answer": "done"}
    with pytest.raises(BoundaryDenied):
        controller.consume_receipt(a, receipt_id)

    with pytest.raises(BoundaryDenied):
        controller.claim(b, root)
    with pytest.raises(BoundaryDenied):
        controller.create_child(b, lease, {"task": "cross-worker"})
    with pytest.raises(BoundaryDenied):
        controller.issue_root(peer("mallory"), {})


def test_claim_is_atomic_and_resume_rotates_the_callback_lease():
    a = peer("alice")
    controller = make_controller(peers={a.worker_id: a}, bindings={"alice": a.binding})
    handle = controller.issue_root(a, {"n": 1})
    barrier = threading.Barrier(3)

    def attempt():
        barrier.wait()
        try:
            return controller.claim(a, handle)
        except BoundaryDenied:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        barrier.wait()
        leases = [future.result() for future in futures]
    assert sum(lease is not None for lease in leases) == 1
    old = next(lease for lease in leases if lease is not None)
    resumed = controller.resume(a, old)
    with pytest.raises(BoundaryDenied):
        controller.complete(a, old, {"late": True})
    assert controller.consume_receipt(a, controller.complete(a, resumed, {"ok": True})) == {"ok": True}


def test_scheduled_jobs_are_generation_pinned_and_do_not_revive_after_regrant():
    now = [10.0]
    a = peer("alice")
    peers, bindings = {a.worker_id: a}, {"alice": a.binding}
    controller = make_controller(peers=peers, bindings=bindings, clock=lambda: now[0])
    handle = controller.schedule(a, {"run": "later"}, delay_seconds=5)
    with pytest.raises(BoundaryDenied):
        controller.claim(a, handle)
    now[0] += 5
    changed = binding("alice", generation="revoked-and-regranted")
    bindings["alice"] = changed
    with pytest.raises(BoundaryDenied):
        controller.claim(a, handle)
    replacement = peer("alice", hashlib.md5(b"alice-new").hexdigest(), generation="revoked-and-regranted")
    peers[replacement.worker_id] = replacement
    # Regranting creates a new authorization generation and does not revive old work.
    with pytest.raises(BoundaryDenied):
        controller.claim(replacement, handle)
    fresh = controller.schedule(replacement, {"run": "new grant"}, delay_seconds=1)
    now[0] += 1
    assert controller.claim(replacement, fresh).payload == {"run": "new grant"}


def test_revocation_invalidates_claim_completion_and_receipt():
    a = peer("alice")
    bindings = {"alice": a.binding}
    controller = make_controller(peers={a.worker_id: a}, bindings=bindings)
    handle = controller.issue_root(a, {})
    lease = controller.claim(a, handle)
    bindings["alice"] = binding("alice", generation="revoked")
    with pytest.raises(BoundaryDenied):
        controller.complete(a, lease, {"late": 1})
    with pytest.raises(BoundaryDenied):
        controller.resume(a, lease)


def test_controller_restart_fails_closed_for_old_handles():
    a = peer("alice")
    peers, bindings = {a.worker_id: a}, {"alice": a.binding}
    first = make_controller(peers=peers, bindings=bindings)
    old_handle = first.issue_root(a, {"work": "in-memory only"})
    restarted = make_controller(peers=peers, bindings=bindings)
    with pytest.raises(BoundaryDenied):
        restarted.claim(a, old_handle)


def test_outbound_is_exact_room_bounded_single_use_and_never_accepts_arbitrary_targets():
    a = peer("alice", thread="topic-7")
    dispatch = Dispatch()
    controller = make_controller(peers={a.worker_id: a}, bindings={"alice": a.binding}, dispatch=dispatch)
    lease = controller.claim(a, controller.issue_root(a, {}))
    target = Destination("room", "forum", "topic-7")
    cap = controller.issue_outbound(a, lease, operation="send_text", destination=target, max_bytes=4)
    with pytest.raises(BoundaryDenied):
        controller.issue_outbound(a, lease, operation="send_text", destination=Destination("other", "forum", "topic-7"))
    with pytest.raises(BoundaryDenied):
        controller.issue_outbound(a, lease, operation="delete_room", destination=target)
    with pytest.raises(BoundaryDenied):
        asyncio.run(controller.publish(a, lease, cap, "five!"))
    # An over-limit attempt does not consume the capability; an authorized send does.
    assert asyncio.run(controller.publish(a, lease, cap, "ok")) == "accepted"
    assert dispatch.sent == [("alice", "room", "forum", "topic-7", "ok")]
    with pytest.raises(BoundaryDenied):
        asyncio.run(controller.publish(a, lease, cap, "ok"))


def test_revocation_during_async_dispatch_rejects_stale_completion():
    async def scenario():
        a = peer("alice")
        bindings = {"alice": a.binding}
        dispatch = Dispatch()
        dispatch.started, dispatch.release = asyncio.Event(), asyncio.Event()
        controller = make_controller(peers={a.worker_id: a}, bindings=bindings, dispatch=dispatch)
        lease = controller.claim(a, controller.issue_root(a, {}))
        cap = controller.issue_outbound(
            a, lease, operation="send_text", destination=Destination("room", "group"),
        )
        pending = asyncio.create_task(controller.publish(a, lease, cap, "hello"))
        await dispatch.started.wait()
        bindings["alice"] = binding("alice", generation="revoked-mid-send")
        dispatch.release.set()
        with pytest.raises(BoundaryDenied):
            await pending
        assert len(dispatch.sent) == 1  # network side effect already started; result is not accepted

    asyncio.run(scenario())


def test_identity_alternation_keeps_jobs_and_receipts_separate_and_quota_is_enforced():
    a, b = peer("alice"), peer("bob")
    controller = make_controller(peers={a.worker_id: a, b.worker_id: b},
                                 bindings={"alice": a.binding, "bob": b.binding},
                                 max_jobs_per_identity=2, max_children_per_job=1)
    a_root = controller.issue_root(a, {"identity": "alice"})
    b_root = controller.issue_root(b, {"identity": "bob"})
    a_lease = controller.claim(a, a_root)
    b_lease = controller.claim(b, b_root)
    a_child = controller.create_child(a, a_lease, {"identity": "alice-child"})
    with pytest.raises(BoundaryDenied):
        controller.issue_root(a, {"over": "quota"})
    b_receipt = controller.complete(b, b_lease, {"identity": "bob"})
    a_receipt = controller.complete(a, controller.claim(a, a_child), {"identity": "alice"})
    assert controller.consume_receipt(a, a_receipt) == {"identity": "alice"}
    assert controller.consume_receipt(b, b_receipt) == {"identity": "bob"}
    with pytest.raises(BoundaryDenied):
        controller.consume_receipt(a, b_receipt)
    with pytest.raises(BoundaryDenied):
        controller.create_child(a, a_lease, {"second": "child"})
    # Identity B retained its own capacity throughout A -> B -> A activity.
    assert controller.issue_root(b, {"still": "independent"})


def test_retained_state_quotas_bound_caps_completed_jobs_and_global_rows_then_prune():
    now = [0.0]
    a, b = peer("alice"), peer("bob")
    controller = make_controller(
        peers={a.worker_id: a, b.worker_id: b},
        bindings={"alice": a.binding, "bob": b.binding},
        clock=lambda: now[0], max_ttl_seconds=60,
        max_retained_per_identity=3, max_retained_total=3,
    )
    handle = controller.issue_root(a, {}, ttl_seconds=2)
    lease = controller.claim(a, handle)
    destination = Destination("room", "group")
    first_cap = controller.issue_outbound(a, lease, operation="send_text", destination=destination)
    controller.issue_outbound(a, lease, operation="send_text", destination=destination)
    with pytest.raises(BoundaryDenied):
        controller.issue_outbound(a, lease, operation="send_text", destination=destination)

    # A different identity cannot exceed the global hard limit either.
    with pytest.raises(BoundaryDenied):
        controller.issue_root(b, {})

    assert asyncio.run(controller.publish(a, lease, first_cap, "sent")) == "accepted"
    # A successfully published one-use cap is discarded, but remains replay-denied.
    with pytest.raises(BoundaryDenied):
        asyncio.run(controller.publish(a, lease, first_cap, "replay"))
    receipt = controller.complete(a, lease, {"done": True})
    assert controller.consume_receipt(a, receipt) == {"done": True}
    with pytest.raises(BoundaryDenied):
        controller.consume_receipt(a, receipt)
    # The completed job and an unused cap remain retained; active-job quota alone
    # cannot permit unbounded churn once the retained-state limit is reached.
    controller.issue_root(a, {})
    with pytest.raises(BoundaryDenied):
        controller.issue_root(a, {})

    now[0] = 3.0
    fresh = controller.issue_root(a, {}, ttl_seconds=2)  # allocation prunes expired rows
    assert controller.claim(a, fresh).payload == {}
    with pytest.raises(BoundaryDenied):
        controller.claim(a, handle)


def test_revoke_peer_invalidates_jobs_receipts_caps_and_is_idempotent_exactly():
    a = peer("alice")
    sibling = peer("alice", "1" * 32)
    dispatch = Dispatch()
    controller = make_controller(
        peers={a.worker_id: a, sibling.worker_id: sibling},
        bindings={"alice": a.binding}, dispatch=dispatch,
    )
    root = controller.issue_root(a, {})
    root_lease = controller.claim(a, root)
    cap = controller.issue_outbound(
        a, root_lease, operation="send_text", destination=Destination("room", "group"),
    )
    child = controller.create_child(a, root_lease, {})
    child_receipt = controller.complete(a, controller.claim(a, child), {"done": True})
    sibling_handle = controller.issue_root(sibling, {})

    controller.revoke_peer(a)
    controller.revoke_peer(a)  # repeated revocation consumes no additional tombstone
    for operation in (
        lambda: controller.claim(a, root),
        lambda: controller.consume_receipt(a, child_receipt),
        lambda: asyncio.run(controller.publish(a, root_lease, cap, "text")),
        lambda: controller.issue_root(a, {}),
    ):
        with pytest.raises(BoundaryDenied):
            operation()
    assert controller.claim(sibling, sibling_handle).payload == {}
    assert dispatch.sent == []


def test_revoke_during_inflight_publish_discards_result_but_not_started_send():
    async def scenario():
        a = peer("alice")
        dispatch = Dispatch()
        dispatch.started, dispatch.release = asyncio.Event(), asyncio.Event()
        controller = make_controller(peers={a.worker_id: a},
                                     bindings={"alice": a.binding}, dispatch=dispatch)
        lease = controller.claim(a, controller.issue_root(a, {}))
        cap = controller.issue_outbound(
            a, lease, operation="send_text", destination=Destination("room", "group"),
        )
        pending = asyncio.create_task(controller.publish(a, lease, cap, "sent"))
        await dispatch.started.wait()
        controller.revoke_peer(a)
        dispatch.release.set()
        with pytest.raises(BoundaryDenied):
            await pending
        assert dispatch.sent == [("alice", "room", "group", None, "sent")]

    asyncio.run(scenario())


def test_schedule_must_be_due_strictly_before_expiry_and_tombstones_are_bounded():
    a = peer("alice")
    other = peer("alice", "1" * 32)
    controller = make_controller(
        peers={a.worker_id: a, other.worker_id: other},
        bindings={"alice": a.binding}, max_ttl_seconds=4,
        max_retained_per_identity=1, max_retained_total=2,
    )
    with pytest.raises(BoundaryDenied):
        controller.schedule(a, {}, delay_seconds=4, ttl_seconds=4)
    controller.revoke_peer(a)
    # The lifetime tombstone itself occupies the per-identity retained-state quota.
    with pytest.raises(BoundaryDenied):
        controller.issue_root(a, {})
    controller.revoke_peer(other)  # no room for this tombstone: fail closed globally
    with pytest.raises(BoundaryDenied):
        controller.issue_root(other, {})
