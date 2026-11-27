"""Controller-state tests, not live Linux containment tests."""
import asyncio
from dataclasses import replace

import pytest

from gateway.isolated_worker import Binding, BoundaryDenied
from gateway.isolated_worker.lifecycle import LinuxCgroups, WorkerLifecycle
from gateway.isolated_worker.supervisor import LaunchPlan, Limits


class Kernel:
    def __init__(self):
        self.calls = []
        self.populated = {}
        self.fail_prepare = False
        self.fail_kill = False
    def prepare(self, plan):
        self.calls.append(("limits", plan.limits))
        if self.fail_prepare:
            raise BoundaryDenied("no delegation")
        self.populated[plan.peer.worker_id] = True
    def kill(self, peer):
        self.calls.append(("kill", peer.worker_id))
        if self.fail_kill:
            raise BoundaryDenied("kernel kill unavailable")
        self.populated[peer.worker_id] = False
    def empty(self, peer):
        return not self.populated[peer.worker_id]
    def release(self, peer):
        assert self.empty(peer)
        self.calls.append(("release", peer.worker_id))
        del self.populated[peer.worker_id]


def plan(identity="11", uid=60001, limits=None):
    return LaunchPlan.create(Binding(identity, "a" * 64, identity, "dm"), uid=uid,
                             runtime_digest="sha256:" + "b" * 64, limits=limits)


def controller(kernel, allowed=None, now=None):
    allowed = allowed if allowed is not None else [True]
    return WorkerLifecycle(kernel, authorize=lambda _: allowed[0],
                           revoke_resources=lambda p: kernel.calls.append(("revoke", p.worker_id)),
                           clock=(lambda: now[0]) if now is not None else __import__("time").monotonic)


@pytest.mark.asyncio
async def test_revoke_kills_all_descendants_before_release_and_tombstones():
    kernel, allowed = Kernel(), [True]
    ctl = controller(kernel, allowed)
    p = plan()
    ctl.register(p)
    allowed[0] = False
    with pytest.raises(BoundaryDenied):
        ctl.check(p.peer)
    assert kernel.calls[-2:] == [("revoke", p.peer.worker_id), ("kill", p.peer.worker_id)]
    await ctl.reap(p.peer)
    assert kernel.calls[-1] == ("release", p.peer.worker_id)
    allowed[0] = True
    with pytest.raises(BoundaryDenied):
        ctl.register(p)
    with pytest.raises(BoundaryDenied):
        ctl.check(p.peer)


@pytest.mark.asyncio
async def test_a_b_a_identity_and_peer_forgery():
    kernel = Kernel(); ctl = controller(kernel)
    a, b = plan(), plan("22", 60002)
    ctl.register(a); ctl.register(b)
    ctl.check(a.peer); ctl.check(b.peer); ctl.check(a.peer)
    with pytest.raises(BoundaryDenied):
        ctl.check(replace(a.peer, binding=b.peer.binding))
    with pytest.raises(BoundaryDenied, match="collision"):
        ctl.register(plan("33", 60001))
    ctl.stop(a.peer); await ctl.reap(a.peer)
    ctl.check(b.peer)
    assert b.peer.worker_id in kernel.populated


def test_expiration_and_limits_precede_execution():
    kernel, now = Kernel(), [10.0]
    ctl = controller(kernel, now=now)
    p = plan(limits=Limits(wall_seconds=1, processes=8))
    ctl.register(p)
    assert kernel.calls[0] == ("limits", p.limits)
    now[0] = 11.0
    with pytest.raises(BoundaryDenied):
        ctl.check(p.peer)
    assert kernel.calls[-1] == ("kill", p.peer.worker_id)


def test_resource_setup_failure_never_admits_worker():
    kernel = Kernel(); kernel.fail_prepare = True
    ctl = controller(kernel); p = plan()
    with pytest.raises(BoundaryDenied):
        ctl.register(p)
    with pytest.raises(BoundaryDenied):
        ctl.check(p.peer)


@pytest.mark.asyncio
async def test_failed_teardown_is_quarantined_not_reused():
    kernel = Kernel(); ctl = controller(kernel); p = plan()
    ctl.register(p); kernel.fail_kill = True
    with pytest.raises(BoundaryDenied):
        ctl.stop(p.peer)
    with pytest.raises(BoundaryDenied):
        ctl.check(p.peer)
    with pytest.raises(BoundaryDenied):
        await ctl.reap(p.peer, timeout=0)
    assert not any(c[0] == "release" for c in kernel.calls)


@pytest.mark.asyncio
async def test_authorization_store_failure_monitor_stops_without_user_code():
    kernel = Kernel(); ctl = controller(kernel); p = plan()
    ctl.register(p)
    def broken(_):
        raise OSError("store unavailable")
    ctl.authorize = broken
    await ctl.monitor()
    assert not kernel.populated
    assert [c[0] for c in kernel.calls] == ["limits", "revoke", "kill", "release"]


def test_real_linux_adapter_rejects_fake_cgroup_and_symlink(tmp_path):
    fake = tmp_path / "cgroup"; fake.mkdir()
    (fake / "cgroup.controllers").write_text("cpu memory pids")
    with pytest.raises(BoundaryDenied, match="cgroup v2"):
        LinuxCgroups(fake)
    link = tmp_path / "alias"; link.symlink_to(fake)
    with pytest.raises(BoundaryDenied):
        LinuxCgroups(link)


@pytest.mark.asyncio
async def test_uid_cannot_be_reassigned_after_identity_teardown():
    kernel = Kernel(); ctl = controller(kernel); a = plan()
    ctl.register(a); ctl.stop(a.peer); await ctl.reap(a.peer)
    with pytest.raises(BoundaryDenied, match="collision"):
        ctl.register(plan("22", 60001))
    fresh_a = plan()
    ctl.register(fresh_a)
    ctl.check(fresh_a.peer)


@pytest.mark.asyncio
async def test_supervisor_capacity_and_tombstones_are_bounded():
    kernel = Kernel()
    ctl = WorkerLifecycle(kernel, authorize=lambda _: True, revoke_resources=lambda _: None,
                          max_workers=1, max_lifetime_workers=1)
    a = plan(); ctl.register(a)
    with pytest.raises(BoundaryDenied):
        ctl.register(plan("22", 60002))
    ctl.stop(a.peer); await ctl.reap(a.peer)
    # Controller lifetime exhausted: restart/provisioning is a trusted decision.
    with pytest.raises(BoundaryDenied):
        ctl.register(plan())
