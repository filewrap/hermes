"""Unenabled whole-agent boundary components.

Nothing here relaxes TenantScope or launches a process from the gateway. Production
activation requires a separately provisioned supervisor and a live release gate.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re


class BoundaryDenied(RuntimeError):
    """Generic failure that must never trigger a local execution fallback."""


@dataclass(frozen=True)
class Binding:
    """Created by trusted ingress, never reconstructed from model claims."""

    identity: str
    authorization_stamp: str
    chat_id: str
    chat_type: str
    thread_id: str | None = None

    def __post_init__(self):
        if (not isinstance(self.identity, str) or not self.identity
                or len(self.identity) > 256
                or not isinstance(self.chat_id, str) or not self.chat_id
                or self.chat_type not in {"dm", "private", "group", "forum"}
                or not isinstance(self.authorization_stamp, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.authorization_stamp)
                or (self.thread_id is not None and not isinstance(self.thread_id, str))):
            raise BoundaryDenied("Invalid worker binding")

    @property
    def key(self) -> str:
        return hashlib.sha256(self.identity.encode()).hexdigest()

    @property
    def room(self) -> str:
        return hashlib.sha256(json.dumps(
            [self.identity, self.chat_id, self.chat_type, self.thread_id],
            separators=(",", ":"),
        ).encode()).hexdigest()


@dataclass(frozen=True)
class Peer:
    """Supervisor-attested peer, not fields supplied by a worker RPC body.

    The transport must derive this from an accepted kernel-authenticated channel
    and the supervisor's immutable worker registry. A PID or a bearer token alone
    is insufficient (PID reuse and token theft).
    """

    worker_id: str
    binding: Binding
    uid: int
    cgroup: str

    def __post_init__(self):
        if (not re.fullmatch(r"[0-9a-f]{32}", self.worker_id)
                or type(self.uid) is not int or self.uid <= 0
                or not isinstance(self.binding, Binding)
                or not re.fullmatch(r"/hermes-workers/[0-9a-f]{32}", self.cgroup)):
            raise BoundaryDenied("Invalid worker peer")
