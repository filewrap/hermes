"""Trusted orchestration primitives for isolated-worker continuations.

This module never executes tenant code and never starts a process. It issues opaque,
controller-local handles to a separately supervised worker and exposes only a narrow,
identity-bound outbound operation. State is intentionally memory-only: a controller
restart invalidates every outstanding handle; this is not a production scheduler.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import secrets
import threading
import time
from typing import Any, Callable, Mapping

from . import Binding, BoundaryDenied, Peer


_ALLOWED_ROOT_KINDS = frozenset({"inline", "background"})
_ALLOWED_OUTBOUND = frozenset({"send_text"})
_ACTIVE = frozenset({"queued", "claimed"})


@dataclass(frozen=True)
class JobLease:
    """An opaque, single-generation execution lease; safe to return to its worker."""

    handle: str
    lease_id: str
    payload: Mapping[str, Any]
    kind: str


@dataclass(frozen=True)
class Destination:
    """Exact room target. Identity is bound separately by the controller."""

    chat_id: str
    chat_type: str
    thread_id: str | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.chat_id, str) or not self.chat_id
                or self.chat_type not in {"dm", "private", "group", "forum"}
                or (self.thread_id is not None and not isinstance(self.thread_id, str))):
            raise BoundaryDenied("Invalid outbound destination")


@dataclass
class _Job:
    handle: str
    peer: Peer
    kind: str
    payload_json: str
    created: float
    expires: float
    not_before: float
    status: str = "queued"
    lease_id: str | None = None
    children: int = 0


@dataclass
class _Receipt:
    peer: Peer
    result_json: str
    expires: float


@dataclass
class _OutboundCapability:
    peer: Peer
    lease_id: str
    destination: Destination
    operation: str
    max_bytes: int
    expires: float
    used: bool = False


class JobController:
    """Fail-closed controller for identity-bound jobs and outbound receipts.

    ``current_binding`` must resolve the currently authorized exact binding from
    trusted gateway state (return ``None`` on denial or uncertainty). ``peer_is_live``
    must attest the supervisor registry/channel, not trust worker-supplied fields.
    Both callbacks are trusted control-plane dependencies; absent, failing, or
    ambiguous answers deny the operation. No tenant Python or host launch callback is
    accepted here.

    The outbound dependency is deliberately narrow: it must provide an async
    ``send_text(identity, chat_id, chat_type, thread_id, text)`` method. It is not
    handed to workers, and no bot/session credential is accepted or returned.
    """

    def __init__(
        self,
        *,
        current_binding: Callable[[str, str, str, str | None], Binding | None],
        peer_is_live: Callable[[Peer], bool],
        outbound: Any,
        clock: Callable[[], float] = time.monotonic,
        max_jobs_per_identity: int = 32,
        max_children_per_job: int = 8,
        max_ttl_seconds: float = 3600,
        max_payload_bytes: int = 64 * 1024,
        max_outbound_bytes: int = 4096,
        max_retained_per_identity: int = 256,
        max_retained_total: int = 4096,
    ) -> None:
        if (not callable(current_binding) or not callable(peer_is_live)
                or type(max_jobs_per_identity) is not int or max_jobs_per_identity < 1
                or type(max_children_per_job) is not int or max_children_per_job < 0
                or not self._positive(max_ttl_seconds)
                or type(max_payload_bytes) is not int or max_payload_bytes < 1
                or type(max_outbound_bytes) is not int or max_outbound_bytes < 1
                or type(max_retained_per_identity) is not int or max_retained_per_identity < 1
                or type(max_retained_total) is not int or max_retained_total < 1
                or max_retained_per_identity > max_retained_total):
            raise ValueError("Invalid job controller limits or trust callbacks")
        self._current_binding = current_binding
        self._peer_is_live = peer_is_live
        self._outbound = outbound
        self._clock = clock
        self._max_jobs = max_jobs_per_identity
        self._max_children = max_children_per_job
        self._max_ttl = float(max_ttl_seconds)
        self._max_payload_bytes = max_payload_bytes
        self._max_outbound_bytes = max_outbound_bytes
        self._max_retained_per_identity = max_retained_per_identity
        self._max_retained_total = max_retained_total
        self._epoch = secrets.token_hex(16)
        self._jobs: dict[str, _Job] = {}
        self._receipts: dict[str, _Receipt] = {}
        self._outbound_caps: dict[str, _OutboundCapability] = {}
        self._revoked_peers: set[Peer] = set()
        self._capacity_exhausted = False
        self._lock = threading.RLock()

    @staticmethod
    def _positive(value: Any) -> bool:
        return type(value) in (int, float) and math.isfinite(value) and value > 0

    def _encode_data(self, value: Any) -> str:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BoundaryDenied("Job data must be bounded JSON data") from exc
        if len(encoded.encode("utf-8")) > self._max_payload_bytes:
            raise BoundaryDenied("Job data exceeds the controller limit")
        return encoded

    @staticmethod
    def _decode_data(encoded: str) -> Any:
        return json.loads(encoded)

    def _authorize(self, peer: Peer) -> None:
        if not isinstance(peer, Peer):
            raise BoundaryDenied("Unattested worker peer")
        if self._capacity_exhausted or peer in self._revoked_peers:
            raise BoundaryDenied("Worker authority is revoked or unavailable")
        try:
            live = self._peer_is_live(peer)
            current = self._current_binding(
                peer.binding.identity, peer.binding.chat_id,
                peer.binding.chat_type, peer.binding.thread_id,
            )
        except Exception as exc:
            raise BoundaryDenied("Worker authority could not be verified") from exc
        if live is not True or current != peer.binding:
            raise BoundaryDenied("Worker authority is stale or denied")

    def _handle(self, handle: str) -> _Job:
        if (not isinstance(handle, str) or not handle.startswith(self._epoch + ".")
                or handle not in self._jobs):
            raise BoundaryDenied("Unknown or expired job handle")
        job = self._jobs[handle]
        if job.expires <= self._clock():
            job.status = "expired"
            job.lease_id = None
            raise BoundaryDenied("Job handle expired")
        return job

    def _validate_job_peer(self, peer: Peer, handle: str) -> _Job:
        self._authorize(peer)
        job = self._handle(handle)
        if job.peer != peer:
            raise BoundaryDenied("Job belongs to another worker")
        return job

    @staticmethod
    def _require_lease(lease: JobLease) -> None:
        if not isinstance(lease, JobLease):
            raise BoundaryDenied("Invalid job lease")

    def _ttl(self, ttl_seconds: float | None) -> float:
        ttl = self._max_ttl if ttl_seconds is None else ttl_seconds
        if not self._positive(ttl) or ttl > self._max_ttl:
            raise BoundaryDenied("Invalid job lifetime")
        return float(ttl)

    def _prune_locked(self, now: float) -> int:
        old_jobs = [key for key, job in self._jobs.items() if job.expires <= now]
        old_receipts = [key for key, receipt in self._receipts.items()
                        if receipt.expires <= now]
        old_caps = [key for key, cap in self._outbound_caps.items()
                    if cap.expires <= now]
        for key in old_jobs:
            del self._jobs[key]
        for key in old_receipts:
            del self._receipts[key]
        for key in old_caps:
            del self._outbound_caps[key]
        return len(old_jobs) + len(old_receipts) + len(old_caps)

    def _ensure_capacity(self, peer: Peer) -> None:
        """Prune before allocation, then bound every retained row and tombstone."""
        self._prune_locked(self._clock())
        if self._capacity_exhausted:
            raise BoundaryDenied("Controller retained-state capacity is exhausted")
        identity = peer.binding.identity
        retained_identity = sum(
            item.peer.binding.identity == identity
            for rows in (self._jobs.values(), self._receipts.values(),
                         self._outbound_caps.values()) for item in rows
        ) + sum(item.binding.identity == identity for item in self._revoked_peers)
        retained_total = (len(self._jobs) + len(self._receipts) + len(self._outbound_caps)
                          + len(self._revoked_peers))
        if (retained_identity >= self._max_retained_per_identity
                or retained_total >= self._max_retained_total):
            raise BoundaryDenied("Retained-state quota exceeded")

    def _allocate(self, peer: Peer, kind: str, payload: Any, *,
                  ttl_seconds: float | None, not_before: float,
                  parent: _Job | None = None) -> str:
        now = self._clock()
        ttl = self._ttl(ttl_seconds)
        expiry = now + ttl
        if parent is not None:
            expiry = min(expiry, parent.expires)
            if parent.children >= self._max_children:
                raise BoundaryDenied("Child job quota exceeded")
        if not_before >= expiry:
            raise BoundaryDenied("Job is not due before its expiry")
        encoded = self._encode_data(payload)
        self._ensure_capacity(peer)
        if (expiry <= self._clock()
                or (parent is not None and (parent.handle not in self._jobs
                                             or parent.expires <= self._clock()))):
            raise BoundaryDenied("Job or parent expired during allocation")
        active = sum(1 for item in self._jobs.values()
                     if item.peer.binding.identity == peer.binding.identity
                     and item.status in _ACTIVE and item.expires > now)
        if active >= self._max_jobs:
            raise BoundaryDenied("Identity job quota exceeded")
        handle = self._epoch + "." + secrets.token_urlsafe(24)
        self._jobs[handle] = _Job(handle, peer, kind, encoded, now, expiry,
                                  not_before, children=0)
        if parent is not None:
            parent.children += 1
        return handle

    def issue_root(self, peer: Peer, payload: Any, *, kind: str = "inline",
                   ttl_seconds: float | None = None) -> str:
        """Issue an inline/background root only to a currently attested exact Peer."""
        if kind not in _ALLOWED_ROOT_KINDS:
            raise BoundaryDenied("Unsupported root job kind")
        with self._lock:
            self._authorize(peer)
            return self._allocate(peer, kind, payload, ttl_seconds=ttl_seconds,
                                  not_before=self._clock())

    def create_child(self, peer: Peer, parent_lease: JobLease, payload: Any, *,
                     ttl_seconds: float | None = None) -> str:
        """Create a child inheriting the parent's exact worker and binding."""
        self._require_lease(parent_lease)
        with self._lock:
            parent = self._validate_job_peer(peer, parent_lease.handle)
            if (parent.status != "claimed" or parent.lease_id != parent_lease.lease_id
                    or parent_lease.kind != parent.kind):
                raise BoundaryDenied("Parent lease is stale")
            return self._allocate(peer, "child", payload, ttl_seconds=ttl_seconds,
                                  not_before=self._clock(), parent=parent)

    def schedule(self, peer: Peer, payload: Any, *, delay_seconds: float,
                 ttl_seconds: float | None = None) -> str:
        """Create one generation-pinned future continuation; it cannot revive on regrant."""
        if not self._positive(delay_seconds) or delay_seconds > self._max_ttl:
            raise BoundaryDenied("Invalid scheduled delay")
        if delay_seconds >= self._ttl(ttl_seconds):
            raise BoundaryDenied("Scheduled delay must be shorter than job lifetime")
        with self._lock:
            self._authorize(peer)
            return self._allocate(peer, "scheduled", payload, ttl_seconds=ttl_seconds,
                                  not_before=self._clock() + float(delay_seconds))

    def claim(self, peer: Peer, handle: str) -> JobLease:
        """Atomically claim one due job after a current authority check."""
        with self._lock:
            job = self._validate_job_peer(peer, handle)
            if job.status != "queued" or job.not_before > self._clock():
                raise BoundaryDenied("Job is unavailable for claim")
            lease_id = secrets.token_urlsafe(24)
            job.status, job.lease_id = "claimed", lease_id
            return JobLease(job.handle, lease_id, self._decode_data(job.payload_json), job.kind)

    def resume(self, peer: Peer, lease: JobLease) -> JobLease:
        """Rotate a claimed lease after rechecking authority; old callbacks go stale."""
        self._require_lease(lease)
        with self._lock:
            job = self._validate_job_peer(peer, lease.handle)
            if (job.status != "claimed" or job.lease_id != lease.lease_id
                    or lease.kind != job.kind):
                raise BoundaryDenied("Job lease is stale or already resumed")
            lease_id = secrets.token_urlsafe(24)
            job.lease_id = lease_id
            return JobLease(job.handle, lease_id, self._decode_data(job.payload_json), job.kind)

    def complete(self, peer: Peer, lease: JobLease, result: Any) -> str:
        """Complete exactly the current lease and mint a single-use bound receipt."""
        self._require_lease(lease)
        with self._lock:
            job = self._validate_job_peer(peer, lease.handle)
            if (job.status != "claimed" or job.lease_id != lease.lease_id
                    or lease.kind != job.kind):
                raise BoundaryDenied("Job completion callback is stale")
            encoded = self._encode_data(result)
            self._ensure_capacity(peer)
            if job.handle not in self._jobs or job.expires <= self._clock():
                raise BoundaryDenied("Job expired during completion")
            job.status, job.lease_id = "complete", None
            receipt_id = self._epoch + "." + secrets.token_urlsafe(24)
            self._receipts[receipt_id] = _Receipt(peer, encoded, job.expires)
            return receipt_id

    def consume_receipt(self, peer: Peer, receipt_id: str) -> Any:
        """Read a completed result once, only from the same current worker/binding."""
        with self._lock:
            self._authorize(peer)
            receipt = self._receipts.get(receipt_id) if isinstance(receipt_id, str) else None
            if (receipt is None or receipt.expires <= self._clock()
                    or receipt.peer != peer):
                raise BoundaryDenied("Receipt is stale, replayed, or belongs elsewhere")
            result = self._decode_data(receipt.result_json)
            del self._receipts[receipt_id]
            return result

    def issue_outbound(self, peer: Peer, lease: JobLease, *, operation: str,
                       destination: Destination, max_bytes: int = 1024,
                       ttl_seconds: float = 60) -> str:
        """Mint a one-use send capability for exactly the job's own room/thread."""
        self._require_lease(lease)
        if operation not in _ALLOWED_OUTBOUND:
            raise BoundaryDenied("Outbound operation is not allowlisted")
        if type(max_bytes) is not int or not 1 <= max_bytes <= self._max_outbound_bytes:
            raise BoundaryDenied("Invalid outbound content bound")
        ttl = self._ttl(ttl_seconds)
        if not isinstance(destination, Destination):
            raise BoundaryDenied("Invalid outbound destination")
        with self._lock:
            job = self._validate_job_peer(peer, lease.handle)
            if (job.status != "claimed" or job.lease_id != lease.lease_id
                    or lease.kind != job.kind):
                raise BoundaryDenied("Outbound capability requires a live job lease")
            binding = peer.binding
            if destination != Destination(binding.chat_id, binding.chat_type, binding.thread_id):
                raise BoundaryDenied("Outbound destination crosses the bound room or thread")
            self._ensure_capacity(peer)
            if job.handle not in self._jobs or job.expires <= self._clock():
                raise BoundaryDenied("Job expired during capability allocation")
            cap = self._epoch + "." + secrets.token_urlsafe(24)
            self._outbound_caps[cap] = _OutboundCapability(
                peer, lease.lease_id, destination, operation, max_bytes,
                min(job.expires, self._clock() + ttl),
            )
            return cap

    async def publish(self, peer: Peer, lease: JobLease, capability: str,
                      content: str) -> Any:
        """Dispatch bounded text through the narrow adapter method, then revalidate.

        A revocation during an already-started network send cannot undo that send; it
        does invalidate the operation's receipt/result and all subsequent use. The
        capability is consumed before awaiting the transport, preventing replay.
        """
        self._require_lease(lease)
        if not isinstance(content, str):
            raise BoundaryDenied("Outbound content must be text")
        content_bytes = len(content.encode("utf-8"))
        with self._lock:
            job = self._validate_job_peer(peer, lease.handle)
            cap = self._outbound_caps.get(capability) if isinstance(capability, str) else None
            if (cap is None or cap.used or cap.expires <= self._clock()
                    or cap.peer != peer or cap.lease_id != lease.lease_id
                    or job.status != "claimed" or job.lease_id != lease.lease_id
                    or lease.kind != job.kind or cap.operation != "send_text"
                    or content_bytes > cap.max_bytes):
                raise BoundaryDenied("Outbound capability is stale, replayed, or invalid")
            cap.used = True
            destination = cap.destination
        try:
            method = getattr(self._outbound, "send_text", None)
            if not callable(method):
                raise BoundaryDenied("Narrow outbound transport is unavailable")
            pending = method(peer.binding.identity, destination.chat_id,
                             destination.chat_type, destination.thread_id, content)
            if not asyncio.iscoroutine(pending) and not isinstance(pending, asyncio.Future):
                raise BoundaryDenied("Outbound transport must be asynchronous")
            result = await pending
        except BoundaryDenied:
            raise
        except Exception as exc:
            raise BoundaryDenied("Outbound dispatch failed") from exc
        with self._lock:
            self._validate_job_peer(peer, lease.handle)
            cap = self._outbound_caps.get(capability)
            if (cap is None or cap.peer != peer or not cap.used
                    or cap.expires <= self._clock()):
                raise BoundaryDenied("Outbound dispatch completed after authority expired")
            del self._outbound_caps[capability]
        return result

    def revoke_peer(self, peer: Peer) -> None:
        """Revoke one exact supervisor-attested peer and discard all its retained state.

        The tombstone prevents the same peer object from becoming usable again if
        external authority is stale or later reappears. If tombstones themselves
        exhaust a hard quota, the whole controller fails closed for its lifetime.
        An outbound send already accepted by the transport cannot be rolled back.
        """
        if not isinstance(peer, Peer):
            raise BoundaryDenied("Unattested worker peer")
        with self._lock:
            self._prune_locked(self._clock())
            for key in [key for key, job in self._jobs.items() if job.peer == peer]:
                del self._jobs[key]
            for key in [key for key, receipt in self._receipts.items()
                        if receipt.peer == peer]:
                del self._receipts[key]
            for key in [key for key, cap in self._outbound_caps.items()
                        if cap.peer == peer]:
                del self._outbound_caps[key]
            if peer in self._revoked_peers or self._capacity_exhausted:
                return
            identity = peer.binding.identity
            retained_identity = sum(
                item.peer.binding.identity == identity
                for rows in (self._jobs.values(), self._receipts.values(),
                             self._outbound_caps.values()) for item in rows
            ) + sum(item.binding.identity == identity for item in self._revoked_peers)
            retained_total = (len(self._jobs) + len(self._receipts)
                              + len(self._outbound_caps) + len(self._revoked_peers))
            if (retained_identity >= self._max_retained_per_identity
                    or retained_total >= self._max_retained_total):
                self._capacity_exhausted = True
                return
            self._revoked_peers.add(peer)

    def prune(self) -> int:
        """Drop expired in-memory rows; restart also invalidates all handles by design."""
        with self._lock:
            return self._prune_locked(self._clock())
