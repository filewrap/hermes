"""External-supervisor lifecycle components, never run by the gateway.

The Linux adapter requires a pre-delegated cgroup v2 subtree. It cannot provision
accounts, change systemd delegation, gain privilege, or launch a host fallback.
Synthetic backend tests are not proof of kernel containment.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time
from typing import Callable, Protocol

from . import BoundaryDenied, Peer
from .supervisor import LaunchPlan


class Cgroups(Protocol):
    def prepare(self, plan: LaunchPlan) -> None: ...
    def kill(self, peer: Peer) -> None: ...
    def empty(self, peer: Peer) -> bool: ...
    def release(self, peer: Peer) -> None: ...


class LinuxCgroups:
    """Operates only an already-delegated real cgroup v2 filesystem.

    A trusted supervisor must attach the child before executing any tenant code
    (e.g. clone3(CLONE_INTO_CGROUP)); moving an already executing child races.
    This adapter intentionally has no unsafe Popen-then-move convenience method.
    """

    def __init__(self, delegated_root: Path):
        root = Path(delegated_root)
        if not root.is_absolute() or root.resolve() != root or root.is_symlink():
            raise BoundaryDenied("Unsafe delegated cgroup path")
        try:
            mounts = []
            for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
                before, after = line.split(" - ", 1)
                mount = Path(before.split()[4])
                if root.is_relative_to(mount):
                    mounts.append((len(str(mount)), after.split()[0]))
            if not mounts or max(mounts)[1] != "cgroup2":
                raise ValueError
            self.root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self.root = root
        except (OSError, ValueError):
            raise BoundaryDenied("Pre-delegated cgroup v2 subtree required") from None

    def close(self):
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1

    def _open(self, peer: Peer) -> int:
        if self.root_fd < 0:
            raise BoundaryDenied("Cgroup adapter closed")
        return os.open(peer.worker_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                       dir_fd=self.root_fd)

    @staticmethod
    def _write(directory: int, name: str, text: str) -> None:
        fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            raw = text.encode("ascii")
            if os.write(fd, raw) != len(raw):
                raise BoundaryDenied("Incomplete cgroup operation")
        finally:
            os.close(fd)

    @staticmethod
    def _read(directory: int, name: str) -> str:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            return os.read(fd, 4096).decode("ascii").strip()
        finally:
            os.close(fd)

    def prepare(self, plan: LaunchPlan) -> None:
        # Dedicated UID, no shared owner profile, and an actual mount/network
        # launcher are additional prerequisites, not supplied by a cgroup.
        created = False
        try:
            os.mkdir(plan.peer.worker_id, mode=0o700, dir_fd=self.root_fd)
            created = True
            fd = self._open(plan.peer)
            try:
                limits = plan.limits
                controls = {"memory.max": str(limits.memory_bytes), "memory.swap.max": "0",
                            "pids.max": str(limits.processes),
                            "cpu.max": f"{limits.cpu_quota_us} {limits.cpu_period_us}"}
                for name, value in controls.items():
                    self._write(fd, name, value)
                    if self._read(fd, name) != value:
                        raise BoundaryDenied("Cgroup limit readback failed")
                # Kernel must support atomic descendant kill before admitting work.
                kill_fd = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(kill_fd)
                if self._read(fd, "cgroup.events").split().count("populated") != 1:
                    raise BoundaryDenied("Cgroup state unavailable")
            finally:
                os.close(fd)
        except Exception:
            if created:
                # Failure to clean up is fatal; never reuse an uncertain group.
                self.kill(plan.peer)
                if self.empty(plan.peer):
                    self.release(plan.peer)
            raise BoundaryDenied("Worker resource boundary unavailable") from None

    def kill(self, peer: Peer) -> None:
        fd = self._open(peer)
        try:
            self._write(fd, "cgroup.kill", "1")
        finally:
            os.close(fd)

    def empty(self, peer: Peer) -> bool:
        fd = self._open(peer)
        try:
            fields = dict(line.split() for line in self._read(fd, "cgroup.events").splitlines())
            if fields.get("populated") not in {"0", "1"}:
                raise BoundaryDenied("Cgroup state unavailable")
            return fields["populated"] == "0"
        finally:
            os.close(fd)

    def release(self, peer: Peer) -> None:
        if not self.empty(peer):
            raise BoundaryDenied("Cannot release live worker descendants")
        os.rmdir(peer.worker_id, dir_fd=self.root_fd)


class WorkerLifecycle:
    """Fail-stop supervision; a failed kill quarantines the slot permanently.

    revoke_resources must synchronously invalidate inference/job/outbound grants
    BEFORE the kernel kill. Authorization monitoring is supervisor-owned, not a
    tenant timer. No callbacks ever execute tenant code in this controller.
    """

    def __init__(self, cgroups: Cgroups, *, authorize: Callable[[Peer], bool],
                 revoke_resources: Callable[[Peer], None], clock=time.monotonic,
                 max_workers: int = 64, max_lifetime_workers: int = 4096):
        if (type(max_workers) is not int or not 1 <= max_workers <= 4096
                or type(max_lifetime_workers) is not int
                or not max_workers <= max_lifetime_workers <= 65536):
            raise BoundaryDenied("Invalid supervisor worker limits")
        self.cgroups = cgroups
        self.authorize = authorize
        self.revoke_resources = revoke_resources
        self.clock = clock
        self.max_workers = max_workers
        self.max_lifetime_workers = max_lifetime_workers
        self._workers: dict[str, tuple[LaunchPlan, float]] = {}
        self._quarantined: set[str] = set()
        self._uid_identities: dict[int, str] = {}
        self._issued: set[str] = set()

    def _authorized(self, peer: Peer) -> bool:
        try:
            return self.authorize(peer) is True
        except Exception:
            return False

    def register(self, plan: LaunchPlan) -> None:
        if (not self._authorized(plan.peer) or plan.peer.worker_id in self._workers
                or plan.peer.worker_id in self._issued
                or len(self._workers) >= self.max_workers
                or len(self._issued) >= self.max_lifetime_workers):
            raise BoundaryDenied("Worker admission denied")
        # Never share a host security UID across identities, even for callbacks.
        if self._uid_identities.get(plan.peer.uid, plan.peer.binding.identity) != plan.peer.binding.identity:
            raise BoundaryDenied("Identity host security account collision")
        self._issued.add(plan.peer.worker_id)
        self._uid_identities[plan.peer.uid] = plan.peer.binding.identity
        self.cgroups.prepare(plan)
        self._workers[plan.peer.worker_id] = (plan, self.clock() + plan.limits.wall_seconds)
        if not self._authorized(plan.peer):
            self.stop(plan.peer)
            raise BoundaryDenied("Worker authorization changed during admission")

    def check(self, peer: Peer) -> None:
        record = self._workers.get(peer.worker_id)
        if record is None or record[0].peer != peer or peer.worker_id in self._quarantined:
            raise BoundaryDenied("Unknown worker")
        if not self._authorized(peer) or self.clock() >= record[1]:
            self.stop(peer)
            raise BoundaryDenied("Worker lease expired or revoked")

    def stop(self, peer: Peer) -> None:
        record = self._workers.get(peer.worker_id)
        if record is None or record[0].peer != peer:
            raise BoundaryDenied("Unknown worker teardown")
        self._quarantined.add(peer.worker_id)
        try:
            self.revoke_resources(peer)
        finally:
            # Revocation failures must not leave executable descendants running.
            self.cgroups.kill(peer)

    async def reap(self, peer: Peer, *, timeout: float = 5) -> None:
        if peer.worker_id not in self._quarantined:
            raise BoundaryDenied("Worker must be stopped before release")
        record = self._workers.get(peer.worker_id)
        if record is None or record[0].peer != peer:
            raise BoundaryDenied("Unknown worker teardown")
        deadline = time.monotonic() + timeout
        while not self.cgroups.empty(peer):
            if time.monotonic() >= deadline:
                raise BoundaryDenied("Worker descendants still running; slot quarantined")
            await asyncio.sleep(0.02)
        self.cgroups.release(peer)
        del self._workers[peer.worker_id]
        # Keep worker IDs tombstoned: old callbacks can never resurrect them.

    async def monitor(self, *, interval: float = 0.1) -> None:
        if not 0 < interval <= 1:
            raise BoundaryDenied("Invalid supervisor monitor interval")
        while self._workers:
            for plan, _ in tuple(self._workers.values()):
                if plan.peer.worker_id not in self._quarantined:
                    try:
                        self.check(plan.peer)
                    except BoundaryDenied:
                        pass  # stop has revoked and killed; reap below proves empty
                if plan.peer.worker_id in self._quarantined:
                    await self.reap(plan.peer)
            if self._workers:
                await asyncio.sleep(interval)
