"""Trusted supervisor contract; no gateway subprocess launcher.

The gateway may submit bounded data over an admin-provisioned Unix socket. It
cannot supply executable paths, mounts, UIDs, environment, cgroups or networks.
This module does NOT provision a supervisor or attest an OS sandbox from JSON.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import socket
import stat
import struct
import uuid
from typing import Callable

from . import Binding, BoundaryDenied, Peer

MAX_FRAME = 2 * 1024 * 1024


@dataclass(frozen=True)
class Limits:
    memory_bytes: int = 1024 * 1024 * 1024
    processes: int = 128
    cpu_quota_us: int = 100000
    cpu_period_us: int = 100000
    wall_seconds: int = 300
    storage_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self):
        ranges = {
            "memory_bytes": (64 * 1024 * 1024, 8 * 1024**3),
            "processes": (4, 512), "cpu_quota_us": (1000, 200000),
            "cpu_period_us": (100000, 100000), "wall_seconds": (1, 3600),
            "storage_bytes": (1024 * 1024, 10 * 1024**3),
        }
        for name, (low, high) in ranges.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise BoundaryDenied("Invalid worker resource limit")


@dataclass(frozen=True)
class WorkerResources:
    """Paths as seen INSIDE the isolated root, never host profile paths.

    Private conversation state is room-scoped, including thread. Global user
    preferences may be explicitly imported by trusted ingress for a DM only.
    """

    binding: Binding

    @property
    def home(self) -> str:
        return "/identity/rooms/" + self.binding.room

    def environment(self) -> dict[str, str]:
        home = self.home
        return {
            "PATH": "/runtime/venv/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "HOME": home, "HERMES_HOME": home,
            "XDG_CONFIG_HOME": home + "/config", "XDG_CACHE_HOME": home + "/cache",
            "XDG_DATA_HOME": home + "/data", "TMPDIR": "/tmp",
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        }

    def directories(self) -> tuple[str, ...]:
        return tuple(self.home + "/" + part for part in (
            "workspace", "memory", "skills", "tasks", "sessions", "browser",
            "mcp", "connectors", "approvals", "processes", "config", "cache", "data",
        ))


@dataclass(frozen=True)
class LaunchPlan:
    """Constructed by an external supervisor from trusted provisioning only."""

    peer: Peer
    runtime_digest: str
    limits: Limits
    resources: WorkerResources

    def __post_init__(self):
        import re
        if (not re.fullmatch(r"sha256:[0-9a-f]{64}", self.runtime_digest)
                or self.peer.binding != self.resources.binding):
            raise BoundaryDenied("Invalid isolated runtime")

    @classmethod
    def create(cls, binding: Binding, *, uid: int, runtime_digest: str,
               limits: Limits | None = None) -> LaunchPlan:
        worker_id = uuid.uuid4().hex
        return cls(Peer(worker_id, binding, uid, "/hermes-workers/" + worker_id),
                   runtime_digest, limits or Limits(), WorkerResources(binding))

    def specification(self) -> dict:
        """Protocol input for an approved supervisor, not proof of enforcement."""
        return {
            "version": 1, "peer": asdict(self.peer),
            "runtime": {"digest": self.runtime_digest, "read_only": True,
                        "exclude": [".git", ".env", "owner_home", "host_sockets"]},
            "namespaces": ["mount", "pid", "ipc", "uts", "user", "network", "cgroup"],
            "security": {"uid": self.peer.uid, "supplementary_groups": [],
                         "no_new_privileges": True, "capabilities": [],
                         "host_proc": False, "sudo": False},
            "mounts": {"/runtime": "verified-readonly-image",
                       "/identity": "private-volume:" + self.peer.binding.key,
                       "/proc": "private-pid-namespace-proc", "/tmp": "bounded-private-tmpfs"},
            "environment": self.resources.environment(),
            "directories": list(self.resources.directories()),
            "limits": asdict(self.limits),
            "network": {"direct_access": False, "dns": "broker-only",
                        "egress": "destination-checked-service", "host_routes": False},
            "inference": {"transport": "peer-bound-inherited-channel", "model": "openrouter/free",
                          "provider_credentials": False},
            "lifecycle": {"kill_cgroup_on_revoke": True, "kill_cgroup_on_disconnect": True,
                          "kill_cgroup_on_timeout": True, "verify_empty_before_release": True},
        }


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = sock.recv(size)
        if not chunk:
            raise BoundaryDenied("Worker supervisor disconnected")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def send_frame(sock: socket.socket, payload: dict) -> None:
    try:
        raw = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        if len(raw) > MAX_FRAME:
            raise ValueError
        sock.sendall(struct.pack("!I", len(raw)) + raw)
    except (OSError, TypeError, ValueError):
        raise BoundaryDenied("Invalid worker protocol frame") from None


def receive_frame(sock: socket.socket) -> dict:
    try:
        size = struct.unpack("!I", _recv_exact(sock, 4))[0]
        if not 0 < size <= MAX_FRAME:
            raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        data = json.loads(_recv_exact(sock, size), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, ValueError, TypeError, struct.error, RecursionError):
        raise BoundaryDenied("Invalid worker protocol frame") from None


class SupervisorClient:
    """Only a separately approved service can launch whole-agent workers.

    There is deliberately no default socket, autostart, subprocess, environment
    feature flag, or fallback to TenantRuntime/local terminal. A socket response
    alone cannot pass the production release gate.
    """

    def __init__(self, socket_path: Path, *, supervisor_uid: int,
                 authorize: Callable[[Binding], bool], timeout: float = 310):
        self.socket_path = Path(socket_path)
        if type(supervisor_uid) is not int or supervisor_uid < 0 or not 0 < timeout <= 3600:
            raise BoundaryDenied("Invalid supervisor configuration")
        self.supervisor_uid = supervisor_uid
        self.authorize = authorize
        self.timeout = timeout

    def _check(self, binding: Binding) -> None:
        try:
            if self.authorize(binding) is not True:
                raise ValueError
        except Exception:
            raise BoundaryDenied("Worker authorization unavailable") from None

    def turn(self, binding: Binding, *, text: str, request_id: str) -> dict:
        self._check(binding)
        if (not isinstance(text, str) or not text or len(text.encode()) > 512 * 1024
                or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            raise BoundaryDenied("Invalid isolated turn")
        try:
            # Directory integrity remains an administrator provisioning invariant;
            # exact SO_PEERCRED after connect prevents socket replacement by a tenant.
            info = self.socket_path.lstat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.supervisor_uid
                    or info.st_mode & stat.S_IWOTH):
                raise BoundaryDenied("Untrusted worker supervisor")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(self.timeout)
                channel.connect(str(self.socket_path))
                _, uid, _ = struct.unpack("3i", channel.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                if uid != self.supervisor_uid:
                    raise BoundaryDenied("Untrusted worker supervisor")
                self._check(binding)
                send_frame(channel, {"version": 1, "operation": "turn", "binding": asdict(binding),
                                     "request_id": request_id, "text": text})
                reply = receive_frame(channel)
                self._check(binding)
                if (set(reply) != {"version", "binding", "request_id", "result"}
                        or type(reply["version"]) is not int or reply["version"] != 1
                        or reply["binding"] != asdict(binding) or reply["request_id"] != request_id
                        or not isinstance(reply["result"], dict)
                        or set(reply["result"]) != {"final_response", "usage"}
                        or not isinstance(reply["result"]["final_response"], str)
                        or not isinstance(reply["result"]["usage"], dict)):
                    raise BoundaryDenied("Invalid isolated turn receipt")
                return reply["result"]
        except BoundaryDenied:
            raise
        except (OSError, ValueError, TypeError):
            raise BoundaryDenied("Isolated worker unavailable; host fallback forbidden") from None


def production_ready() -> bool:
    """False until separate host provisioning AND the live release gate pass.

    Not configurable from a worker, user preference, or environment variable.
    The existing gateway does not call this client and remains tool-disabled.
    """
    return False
